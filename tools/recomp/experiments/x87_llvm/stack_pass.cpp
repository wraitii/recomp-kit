// Bounded stack-to-SSA pass. The helpers define the current runtime semantics;
// this pass changes representation, never arithmetic. See README.md for proof.
#include "llvm/IR/IRBuilder.h"
#include "llvm/IR/PassManager.h"
#include "llvm/Passes/PassBuilder.h"
#include "llvm/Plugins/PassPlugin.h"
#include <array>

using namespace llvm;

namespace {
class X87StackPass : public PassInfoMixin<X87StackPass> {
  public:
    PreservedAnalyses run(Function &F, FunctionAnalysisManager &) {
        if (!F.hasFnAttribute("recomp.x87.region"))
            return PreservedAnalyses::all();
        auto Reject = [&](const char *Message) {
            F.getContext().emitError(Message);
            return PreservedAnalyses::all();
        };
        // Validate the complete region BEFORE editing it. No unknown operation
        // may observe or replace CPU state while the pass holds local values.
        if (F.size() != 1 || F.arg_size() != 1 || !F.getReturnType()->isVoidTy() ||
            !F.getArg(0)->getType()->isPointerTy())
            return Reject("x87 region requires one block and void(ptr) ABI");
        int Depth = 0;
        for (Instruction &I : F.front()) {
            if (auto *C = dyn_cast<CallInst>(&I)) {
                auto *Callee = C->getCalledFunction();
                if (!Callee || !C->arg_size() || C->getArgOperand(0) != F.getArg(0))
                    return Reject("x87 region has an indirect call or different CPU pointer");
                StringRef N = Callee->getName();
                if (N != "rk_push" && N != "rk_pop" && N != "rk_read" && N != "rk_set" &&
                    N != "rk_reg" && N != "rk_load" && N != "rk_store" && N != "rk_round")
                    return Reject("unsupported call in x87 region");
                // These names are a private semantic ABI, not arbitrary calls
                // with user-supplied definitions. Link their bodies afterwards.
                unsigned Args = N == "rk_pop" ? 1 : (N == "rk_store" || N == "rk_set" ? 3 : 2);
                bool ReturnsDouble = N == "rk_read" || N == "rk_load" || N == "rk_round";
                Type *Result = ReturnsDouble   ? Type::getDoubleTy(F.getContext())
                               : N == "rk_reg" ? Type::getInt32Ty(F.getContext())
                                               : Type::getVoidTy(F.getContext());
                if (!Callee->isDeclaration() || Callee->isVarArg() || C->arg_size() != Args ||
                    C->getType() != Result || C->isMustTailCall() || C->hasOperandBundles())
                    return Reject("invalid x87 semantic helper declaration");
                if (Args >= 2) {
                    bool DoubleArg = N == "rk_push" || N == "rk_round";
                    Type *Expected = DoubleArg ? Type::getDoubleTy(F.getContext())
                                               : Type::getInt32Ty(F.getContext());
                    if (C->getArgOperand(1)->getType() != Expected ||
                        (Args == 3 && !C->getArgOperand(2)->getType()->isDoubleTy()))
                        return Reject("invalid x87 semantic helper argument");
                }
                if (N == "rk_reg") {
                    auto *Index = dyn_cast<ConstantInt>(C->getArgOperand(1));
                    if (!Index || Index->getZExtValue() >= 8)
                        return Reject("invalid guest register index");
                }
                if (N == "rk_push") {
                    if (++Depth > 8)
                        return Reject("x87 local stack overflow");
                } else if (N == "rk_pop") {
                    if (--Depth < 0)
                        return Reject("x87 incoming stack dependency");
                } else if (N == "rk_read" || N == "rk_set") {
                    auto *Index = dyn_cast<ConstantInt>(C->getArgOperand(1));
                    if (!Index || Index->getZExtValue() >= unsigned(Depth))
                        return Reject("x87 read/set requires a locally defined stack slot");
                } else if (N != "rk_reg" && N != "rk_load" && N != "rk_store" && N != "rk_round") {
                    return Reject("unsupported call in x87 region");
                }
            } else if (auto *B = dyn_cast<BinaryOperator>(&I)) {
                bool Integer =
                    (B->getOpcode() == Instruction::Add || B->getOpcode() == Instruction::Mul) &&
                    B->getType()->isIntegerTy(32) && !B->hasNoSignedWrap() &&
                    !B->hasNoUnsignedWrap();
                bool Float =
                    (B->getOpcode() == Instruction::FAdd || B->getOpcode() == Instruction::FSub ||
                     B->getOpcode() == Instruction::FMul) &&
                    B->getType()->isDoubleTy() && !B->getFastMathFlags().any();
                if (!Integer && !Float)
                    return Reject("unsupported arithmetic or relaxation in x87 region");
            } else if (!isa<ReturnInst>(I)) {
                return Reject("unsupported instruction in x87 region");
            }
        }

        LLVMContext &Ctx = F.getContext();
        Module *M = F.getParent();
        Type *Ptr = PointerType::getUnqual(Ctx), *I32 = Type::getInt32Ty(Ctx);
        Type *Void = Type::getVoidTy(Ctx), *Double = Type::getDoubleTy(Ctx);
        auto TopFn = M->getOrInsertFunction("rk_top", I32, Ptr);
        auto SlotFn = M->getOrInsertFunction("rk_slot", Void, Ptr, I32, Double, I32);
        auto SetTopFn = M->getOrInsertFunction("rk_set_top", Void, Ptr, I32);
        IRBuilder<> Entry(&*F.front().begin());
        Value *CPU = F.getArg(0), *Top = Entry.CreateCall(TopFn, {CPU}, "entry.top");
        // Position k denotes physical slot (entry TOP - k - 1) & 7.
        // Last retains popped contents: dropping a value does not erase it.
        std::array<Value *, 8> Last{};
        Depth = 0;
        SmallVector<Instruction *, 32> Erase;
        for (Instruction &I : F.front()) {
            auto *C = dyn_cast<CallInst>(&I);
            if (C) {
                StringRef N = C->getCalledFunction()->getName();
                if (N == "rk_push") {
                    Last[Depth++] = C->getArgOperand(1);
                    Erase.push_back(C);
                } else if (N == "rk_pop") {
                    --Depth;
                    Erase.push_back(C);
                } else if (N == "rk_read") {
                    unsigned Index = cast<ConstantInt>(C->getArgOperand(1))->getZExtValue();
                    C->replaceAllUsesWith(Last[Depth - 1 - Index]);
                    Erase.push_back(C);
                } else if (N == "rk_set") {
                    unsigned Index = cast<ConstantInt>(C->getArgOperand(1))->getZExtValue();
                    Last[Depth - 1 - Index] = C->getArgOperand(2);
                    Erase.push_back(C);
                }
            } else if (isa<ReturnInst>(I)) {
                IRBuilder<> B(&I);
                for (unsigned K = 0; K < Last.size(); ++K) {
                    if (!Last[K])
                        continue;
                    Value *Phys = B.CreateAnd(B.CreateSub(Top, B.getInt32(K + 1)), B.getInt32(7));
                    B.CreateCall(SlotFn, {CPU, Phys, Last[K], B.getInt32(K < unsigned(Depth))});
                }
                B.CreateCall(SetTopFn, {CPU, B.CreateAnd(B.CreateSub(Top, B.getInt32(Depth)),
                                                         B.getInt32(7))});
            }
        }
        for (Instruction *I : Erase)
            I->eraseFromParent();
        F.removeFnAttr("recomp.x87.region");
        return PreservedAnalyses::none();
    }
};
} // namespace

extern "C" LLVM_ATTRIBUTE_WEAK PassPluginLibraryInfo llvmGetPassPluginInfo() {
    return {LLVM_PLUGIN_API_VERSION, "RecompX87", LLVM_VERSION_STRING, [](PassBuilder &PB) {
                PB.registerPipelineParsingCallback([](StringRef Name, FunctionPassManager &PM,
                                                      ArrayRef<PassBuilder::PipelineElement>) {
                    if (Name != "recomp-x87-stack")
                        return false;
                    PM.addPass(X87StackPass());
                    return true;
                });
            }};
}

// Bounded CFG stack-to-SSA passes. See README.md for each preservation contract.
#include "llvm/IR/CFG.h"
#include "llvm/IR/IRBuilder.h"
#include "llvm/IR/PassManager.h"
#include "llvm/IR/Operator.h"
#include "llvm/ADT/SmallPtrSet.h"
#include "llvm/Passes/PassBuilder.h"
#include "llvm/Plugins/PassPlugin.h"
#include <array>
#include <functional>

using namespace llvm;
namespace {
struct Shape {
    unsigned Depth = 0, Touched = 0;
    bool operator==(const Shape &Other) const {
        return Depth == Other.Depth && Touched == Other.Touched;
    }
};
struct StackPlan {
    bool Valid = false;
    SmallVector<BasicBlock *, 16> Order;
    DenseMap<BasicBlock *, Shape> In, Out;
    SmallPtrSet<BasicBlock *, 8> Sync;
};

// A closed semantic ABI: declarations only, exact types, and one CPU object.
// Observers may read x87 state but cannot modify it (FNSTSW writes only EAX).
static bool validCall(CallInst &C, Function &F) {
    auto *Fn = C.getCalledFunction();
    if (!Fn || !Fn->isDeclaration() || Fn->isVarArg() || C.isMustTailCall() ||
        C.hasOperandBundles() || !C.arg_size() || C.getArgOperand(0) != F.getArg(0))
        return false;
    if (isa<FPMathOperator>(C) && C.getFastMathFlags().any())
        return false;
    LLVMContext &Ctx = F.getContext();
    Type *P = F.getArg(0)->getType(), *D = Type::getDoubleTy(Ctx);
    Type *U = Type::getInt32Ty(Ctx), *V = Type::getVoidTy(Ctx);
    StringRef N = Fn->getName();
    // Direct accesses have a different, explicit environment contract: mapped
    // memory disjoint from CPU/runtime storage, no access observers or faults.
    if (N.starts_with("rk_direct_") && !F.hasFnAttribute("recomp.x87.direct"))
        return false;
    FunctionType *Expected = nullptr;
    if (N == "rk_push")
        Expected = FunctionType::get(V, {P, D}, false);
    if (N == "rk_pop" || N == "rk_fnstsw" || N == "rk_observe" || N == "rk_direct_fnstsw")
        Expected = FunctionType::get(V, {P}, false);
    if (N == "rk_read" || N == "rk_load" || N == "rk_load64" || N == "rk_direct_load" ||
        N == "rk_direct_load64")
        Expected = FunctionType::get(D, {P, U}, false);
    if (N == "rk_reg" || N == "rk_zf" || N == "rk_direct_load32")
        Expected = FunctionType::get(U, {P, U}, false);
    if (N == "rk_direct_pop32")
        Expected = FunctionType::get(U, {P}, false);
    if (N == "rk_set" || N == "rk_store" || N == "rk_direct_store")
        Expected = FunctionType::get(V, {P, U, D}, false);
    if (N == "rk_round")
        Expected = FunctionType::get(D, {P, D}, false);
    if (N == "rk_compare")
        Expected = FunctionType::get(V, {P, D, D}, false);
    if (N == "rk_test_ah" || N == "rk_xor_eax" || N == "rk_ret" || N == "rk_direct_ret" ||
        N == "rk_direct_push32" || N == "rk_inc32" || N == "rk_dec32")
        Expected = FunctionType::get(V, {P, U}, false);
    if (N == "rk_write_reg" || N == "rk_cmp32" || N == "rk_direct_call")
        Expected = FunctionType::get(V, {P, U, U}, false);
    if (N == "rk_direct_call" && !F.hasFnAttribute("recomp.x87.sync"))
        return false;
    return Expected && C.getFunctionType() == Expected && Fn->getFunctionType() == Expected;
}

// Pass 2: validate everything before mutation; propagate depth and the set of
// written physical positions. Optional synchronization cuts every DFS backedge
// at its target. No local stack value may cross such a cut; all predecessors
// must materialize, and the target starts with current physical state.
class X87Analysis : public AnalysisInfoMixin<X87Analysis> {
    friend AnalysisInfoMixin<X87Analysis>;
    static AnalysisKey Key;

  public:
    using Result = StackPlan;
    Result run(Function &F, FunctionAnalysisManager &) {
        StackPlan P;
        if (!F.hasFnAttribute("recomp.x87.region"))
            return P;
        auto Reject = [&](const char *Message) {
            F.getContext().emitError(Message);
            StackPlan Invalid;
            return Invalid;
        };
        if (F.empty() || F.arg_size() != 1 || !F.getReturnType()->isVoidTy() ||
            !F.getArg(0)->getType()->isPointerTy() || !pred_empty(&F.front()))
            return Reject("x87 region requires entry without predecessors and void(ptr) ABI");
        bool SyncCFG = F.hasFnAttribute("recomp.x87.sync");
        if (SyncCFG && !F.hasFnAttribute("recomp.x87.direct"))
            return Reject("synchronized CFG requires direct-access contract");
        if (SyncCFG) {
            DenseMap<BasicBlock *, unsigned> Color;
            std::function<void(BasicBlock *)> Visit = [&](BasicBlock *B) {
                Color[B] = 1;
                for (BasicBlock *Succ : successors(B)) {
                    if (Color[Succ] == 1)
                        P.Sync.insert(Succ);
                    else if (!Color[Succ])
                        Visit(Succ);
                }
                Color[B] = 2;
            };
            Visit(&F.front());
            if (Color.size() != F.size())
                return Reject("x87 CFG has an unreachable block");
        }
        DenseMap<BasicBlock *, unsigned> Pending;
        for (BasicBlock &B : F) {
            Pending[&B] = P.Sync.contains(&B) ? 0 : pred_size(&B);
            if (&B == &F.front() || P.Sync.contains(&B))
                P.Order.push_back(&B);
        }
        for (unsigned B = 0; B < P.Order.size(); ++B)
            for (BasicBlock *Succ : successors(P.Order[B]))
                if (!P.Sync.contains(Succ) && --Pending[Succ] == 0)
                    P.Order.push_back(Succ);
        if (P.Order.size() != F.size())
            return Reject("x87 CFG has a cycle or unreachable block");
        for (BasicBlock *B : P.Order) {
            Shape S;
            if (B != &F.front() && !P.Sync.contains(B)) {
                S = P.Out[*pred_begin(B)];
                bool Same = true, Empty = true;
                for (BasicBlock *Pred : predecessors(B)) {
                    Same &= P.Out[Pred] == S;
                    Empty &= P.Out[Pred].Depth == 0;
                }
                if (!Same) {
                    if (!SyncCFG || !Empty)
                        return Reject("incompatible x87 join depth or touched slots");
                    P.Sync.insert(B);
                    S = {};
                }
            }
            P.In[B] = S;
            for (Instruction &I : *B) {
                if (auto *C = dyn_cast<CallInst>(&I)) {
                    if (!validCall(*C, F))
                        return Reject(
                            "unsupported call or invalid x87 semantic helper declaration");
                    StringRef N = C->getCalledFunction()->getName();
                    if (N == "rk_reg" || N == "rk_write_reg" || N == "rk_inc32" ||
                        N == "rk_dec32") {
                        auto *Index = dyn_cast<ConstantInt>(C->getArgOperand(1));
                        if (!Index || Index->getZExtValue() >= 8)
                            return Reject("invalid guest register index");
                    }
                    if (N == "rk_direct_call") {
                        if (S.Depth)
                            return Reject("guest call requires empty local x87 stack");
                        S = {}; // A callee may replace TOP/CW/slots and metadata.
                    } else if (N == "rk_push") {
                        if (S.Depth == 8)
                            return Reject("x87 local stack overflow");
                        S.Touched |= 1u << S.Depth++;
                    } else if (N == "rk_pop") {
                        if (!S.Depth)
                            return Reject("x87 incoming stack dependency");
                        --S.Depth;
                    } else if (N == "rk_read" || N == "rk_set") {
                        auto *Index = dyn_cast<ConstantInt>(C->getArgOperand(1));
                        if (!Index || Index->getZExtValue() >= S.Depth)
                            return Reject("x87 read/set requires a locally defined stack slot");
                    }
                    if ((N == "rk_ret" || N == "rk_direct_ret") &&
                        !isa<ReturnInst>(C->getNextNode()))
                        return Reject("guest return must immediately precede LLVM return");
                } else if (auto *Op = dyn_cast<BinaryOperator>(&I)) {
                    bool Integer = (Op->getOpcode() == Instruction::Add ||
                                    Op->getOpcode() == Instruction::Mul) &&
                                   Op->getType()->isIntegerTy(32) && !Op->hasNoSignedWrap() &&
                                   !Op->hasNoUnsignedWrap();
                    bool Float = (Op->getOpcode() == Instruction::FAdd ||
                                  Op->getOpcode() == Instruction::FSub ||
                                  Op->getOpcode() == Instruction::FMul) &&
                                 Op->getType()->isDoubleTy() && !Op->getFastMathFlags().any();
                    if (!Integer && !Float)
                        return Reject("unsupported arithmetic or relaxation in x87 region");
                } else if (auto *Op = dyn_cast<UnaryOperator>(&I)) {
                    if (Op->getOpcode() != Instruction::FNeg || !Op->getType()->isDoubleTy() ||
                        Op->getFastMathFlags().any())
                        return Reject("unsupported unary arithmetic or relaxation");
                } else if (auto *Cmp = dyn_cast<ICmpInst>(&I)) {
                    if (!Cmp->getOperand(0)->getType()->isIntegerTy(32))
                        return Reject("unsupported comparison");
                } else if (!isa<ReturnInst>(I) && !isa<BranchInst>(I)) {
                    return Reject("unsupported instruction in x87 region");
                }
            }
            P.Out[B] = S;
        }
        for (BasicBlock *B : P.Sync)
            for (BasicBlock *Pred : predecessors(B))
                if (P.Out[Pred].Depth)
                    return Reject("synchronization requires empty local x87 stack");
        P.Valid = true;
        return P;
    }
};
AnalysisKey X87Analysis::Key;

// Pass 3: construct PHIs for last-written values, including popped contents.
// Snapshot calls describe deferred state at every explicit observer and exit;
// numeric and memory instructions stay in their original blocks and order.
class X87SSAPass : public PassInfoMixin<X87SSAPass> {
  public:
    PreservedAnalyses run(Function &F, FunctionAnalysisManager &AM) {
        if (!F.hasFnAttribute("recomp.x87.region"))
            return PreservedAnalyses::all();
        const StackPlan &P = AM.getResult<X87Analysis>(F);
        if (!P.Valid)
            return PreservedAnalyses::all();
        LLVMContext &Ctx = F.getContext();
        Type *Ptr = F.getArg(0)->getType(), *U = Type::getInt32Ty(Ctx), *D = Type::getDoubleTy(Ctx);
        auto TopFn = F.getParent()->getOrInsertFunction("rk_top", U, Ptr);
        SmallVector<Type *, 12> Args{Ptr, U, U, U};
        Args.append(8, D);
        auto Snapshot = F.getParent()->getOrInsertFunction(
            "rk_snapshot", FunctionType::get(Type::getVoidTy(Ctx), Args, false));
        Value *CPU = F.getArg(0);
        using Values = std::array<Value *, 8>;
        DenseMap<BasicBlock *, Values> Out;
        DenseMap<BasicBlock *, Value *> OutTop;
        SmallVector<Instruction *, 32> Erase;
        for (BasicBlock *BB : P.Order) {
            Shape S = P.In.lookup(BB);
            Value *Top;
            if (BB == &F.front() || P.Sync.contains(BB)) {
                IRBuilder<> Entry(&*BB->begin());
                Top = Entry.CreateCall(TopFn, {CPU}, "region.top");
            } else if (pred_size(BB) == 1)
                Top = OutTop[*pred_begin(BB)];
            else {
                auto *Phi = PHINode::Create(U, pred_size(BB), "x87.top", BB->begin());
                for (BasicBlock *Pred : predecessors(BB))
                    Phi->addIncoming(OutTop[Pred], Pred);
                Top = Phi;
            }
            Values Last{};
            for (unsigned K = 0; K < 8; ++K) {
                if (!(S.Touched & (1u << K)))
                    continue;
                if (pred_size(BB) == 1)
                    Last[K] = Out[*pred_begin(BB)][K];
                else {
                    auto *Phi = PHINode::Create(D, pred_size(BB), "x87.last", BB->begin());
                    for (BasicBlock *Pred : predecessors(BB))
                        Phi->addIncoming(Out[Pred][K], Pred);
                    Last[K] = Phi;
                }
            }
            auto Checkpoint = [&](Instruction &I) {
                IRBuilder<> B(&I);
                SmallVector<Value *, 12> V{CPU, Top, B.getInt32(S.Depth), B.getInt32(S.Touched)};
                for (Value *L : Last)
                    V.push_back(L ? L : ConstantFP::get(D, 0));
                B.CreateCall(Snapshot, V);
            };
            for (Instruction &I : *BB) {
                if (auto *C = dyn_cast<CallInst>(&I)) {
                    StringRef N = C->getCalledFunction()->getName();
                    if (N == "rk_push") {
                        S.Touched |= 1u << S.Depth;
                        Last[S.Depth++] = C->getArgOperand(1);
                        Erase.push_back(C);
                    } else if (N == "rk_pop") {
                        --S.Depth;
                        Erase.push_back(C);
                    } else if (N == "rk_read" || N == "rk_set") {
                        unsigned Index = cast<ConstantInt>(C->getArgOperand(1))->getZExtValue();
                        Value *&Slot = Last[S.Depth - 1 - Index];
                        if (N == "rk_read")
                            C->replaceAllUsesWith(Slot);
                        else
                            Slot = C->getArgOperand(2);
                        Erase.push_back(C);
                    } else if (N == "rk_direct_call") {
                        Checkpoint(I);
                        IRBuilder<> After(C->getNextNode());
                        Top = After.CreateCall(TopFn, {CPU}, "after.call.top");
                        S = {};
                        Last = {};
                    } else if (N == "rk_fnstsw" || N == "rk_observe" || N == "rk_ret" ||
                               N == "rk_load" || N == "rk_load64" || N == "rk_store" ||
                               N.starts_with("rk_direct_")) {
                        Checkpoint(I);
                    }
                } else if (isa<BranchInst>(I)) {
                    for (BasicBlock *Succ : successors(BB))
                        if (P.Sync.contains(Succ)) {
                            Checkpoint(I);
                            break;
                        }
                } else if (isa<ReturnInst>(I)) {
                    auto *Prev = dyn_cast_or_null<CallInst>(I.getPrevNode());
                    if (!Prev || (Prev->getCalledFunction()->getName() != "rk_ret" &&
                                  Prev->getCalledFunction()->getName() != "rk_direct_ret"))
                        Checkpoint(I);
                }
            }
            Out[BB] = Last;
            OutTop[BB] = Top;
        }
        for (Instruction *I : Erase)
            I->eraseFromParent();
        F.removeFnAttr("recomp.x87.region");
        F.addFnAttr("recomp.x87.ssa");
        return PreservedAnalyses::none();
    }
};

// Pass 4: discharge state demands before lowering snapshots to stores.
// The closed helper ABI is the effect summary: direct memory reads no x87
// state; plain FNSTSW reads only SW and TOP; observers/dispatch/exits read all.
// SW stays current in memory. Supply virtual TOP to FNSTSW as an SSA operand,
// so neither its stack slots nor TOP need a physical write at that point.
class X87EffectsPass : public PassInfoMixin<X87EffectsPass> {
  public:
    PreservedAnalyses run(Function &F, FunctionAnalysisManager &) {
        if (!F.hasFnAttribute("recomp.x87.ssa") || !F.hasFnAttribute("recomp.x87.effects"))
            return PreservedAnalyses::all();
        if (!F.hasFnAttribute("recomp.x87.direct")) {
            F.getContext().emitError("x87 effects requires the direct-access contract");
            return PreservedAnalyses::all();
        }
        Type *V = Type::getVoidTy(F.getContext()), *U = Type::getInt32Ty(F.getContext());
        auto Status =
            F.getParent()->getOrInsertFunction("rk_status_at", V, F.getArg(0)->getType(), U);
        SmallVector<Instruction *, 16> Erase;
        for (BasicBlock &BB : F)
            for (Instruction &I : BB) {
                auto *Snapshot = dyn_cast<CallInst>(&I);
                if (!Snapshot || Snapshot->getCalledFunction()->getName() != "rk_snapshot")
                    continue;
                auto *Next = dyn_cast_or_null<CallInst>(I.getNextNode());
                if (!Next)
                    continue; // LLVM return: preserve complete final state.
                StringRef N = Next->getCalledFunction()->getName();
                if (N == "rk_direct_load" || N == "rk_direct_load64" || N == "rk_direct_store" ||
                    N == "rk_direct_load32" || N == "rk_direct_push32" || N == "rk_direct_pop32")
                    Erase.push_back(Snapshot);
                else if (N == "rk_direct_fnstsw") {
                    IRBuilder<> B(Next);
                    Value *Top = B.CreateAnd(
                        B.CreateSub(Snapshot->getArgOperand(1), Snapshot->getArgOperand(2)),
                        B.getInt32(7));
                    B.CreateCall(Status, {F.getArg(0), Top});
                    Erase.push_back(Snapshot);
                    Erase.push_back(Next);
                }
                // All other boundaries retain their complete snapshot. In
                // particular an explicit observer is never inferred to be dead.
            }
        for (Instruction *I : Erase)
            I->eraseFromParent();
        F.removeFnAttr("recomp.x87.effects");
        return PreservedAnalyses::none();
    }
};

// Pass 5: lower explicit snapshots to complete physical slot/TOP writes.
// Untouched slots remain intact; all touched slots retain their final contents.
class X87MaterializePass : public PassInfoMixin<X87MaterializePass> {
  public:
    PreservedAnalyses run(Function &F, FunctionAnalysisManager &) {
        if (!F.hasFnAttribute("recomp.x87.ssa"))
            return PreservedAnalyses::all();
        LLVMContext &Ctx = F.getContext();
        Type *Ptr = F.getArg(0)->getType(), *U = Type::getInt32Ty(Ctx), *D = Type::getDoubleTy(Ctx);
        Type *V = Type::getVoidTy(Ctx);
        auto SlotFn = F.getParent()->getOrInsertFunction("rk_slot", V, Ptr, U, D, U);
        auto TopFn = F.getParent()->getOrInsertFunction("rk_set_top", V, Ptr, U);
        SmallVector<Instruction *, 16> Erase;
        for (BasicBlock &BB : F)
            for (Instruction &I : BB) {
                auto *C = dyn_cast<CallInst>(&I);
                if (!C || C->getCalledFunction()->getName() != "rk_snapshot")
                    continue;
                IRBuilder<> B(C);
                Value *CPU = C->getArgOperand(0), *Top = C->getArgOperand(1);
                unsigned Depth = cast<ConstantInt>(C->getArgOperand(2))->getZExtValue();
                unsigned Mask = cast<ConstantInt>(C->getArgOperand(3))->getZExtValue();
                for (unsigned K = 0; K < 8; ++K)
                    if (Mask & (1u << K)) {
                        Value *Phys =
                            B.CreateAnd(B.CreateSub(Top, B.getInt32(K + 1)), B.getInt32(7));
                        B.CreateCall(SlotFn,
                                     {CPU, Phys, C->getArgOperand(K + 4), B.getInt32(K < Depth)});
                    }
                B.CreateCall(
                    TopFn, {CPU, B.CreateAnd(B.CreateSub(Top, B.getInt32(Depth)), B.getInt32(7))});
                Erase.push_back(C);
            }
        for (Instruction *I : Erase)
            I->eraseFromParent();
        F.removeFnAttr("recomp.x87.ssa");
        return PreservedAnalyses::none();
    }
};
} // namespace

extern "C" LLVM_ATTRIBUTE_WEAK PassPluginLibraryInfo llvmGetPassPluginInfo() {
    return {LLVM_PLUGIN_API_VERSION, "RecompX87", LLVM_VERSION_STRING, [](PassBuilder &PB) {
                PB.registerAnalysisRegistrationCallback([](FunctionAnalysisManager &AM) {
                    AM.registerPass([] { return X87Analysis(); });
                });
                PB.registerPipelineParsingCallback([](StringRef Name, FunctionPassManager &PM,
                                                      ArrayRef<PassBuilder::PipelineElement>) {
                    if (Name == "recomp-x87-analyze")
                        PM.addPass(RequireAnalysisPass<X87Analysis, Function>());
                    else if (Name == "recomp-x87-ssa")
                        PM.addPass(X87SSAPass());
                    else if (Name == "recomp-x87-effects")
                        PM.addPass(X87EffectsPass());
                    else if (Name == "recomp-x87-materialize")
                        PM.addPass(X87MaterializePass());
                    else if (Name == "recomp-x87-stack") {
                        PM.addPass(RequireAnalysisPass<X87Analysis, Function>());
                        PM.addPass(X87SSAPass());
                        PM.addPass(X87EffectsPass());
                        PM.addPass(X87MaterializePass());
                    } else
                        return false;
                    return true;
                });
            }};
}

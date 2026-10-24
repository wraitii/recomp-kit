# bug_geteip -- call to the immediately following instruction (get-EIP) is lowered
# as a jump, so the callee's pushed return address is never popped.
# xfail: call to the next instruction is lowered as a jump to a non-entry

.text
.globl bug_geteip
.type bug_geteip, @function
bug_geteip:
    call geteip_next
geteip_next:
    pop eax
    add eax, [esp + 4]
    ret
.size bug_geteip, .-bug_geteip

# input: arg0=0
# input: arg0=1
# input: arg0=0xffffffff

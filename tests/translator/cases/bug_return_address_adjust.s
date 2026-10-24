# bug_return_address_adjust -- a callee increments its own return address to skip an
# inline add; the translated return still executes the skipped instruction.
# xfail: RET to a non-continuation address falls back to the host return

.text
.globl bug_return_address_adjust
.type bug_return_address_adjust, @function
bug_return_address_adjust:
    mov ecx, [esp + 4]
    push ecx
    call skip_increment
    add eax, 0x12345678
    pop ecx
    add eax, ecx
    ret
.size bug_return_address_adjust, .-bug_return_address_adjust

.type skip_increment, @function
skip_increment:
    mov eax, [esp + 4]
    add dword ptr [esp], 5
    ret
.size skip_increment, .-skip_increment

# input: arg0=0
# input: arg0=1
# input: arg0=0xffffffff

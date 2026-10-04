"""Focused synthetic CFG: independently selected live and retained popped values."""

BODY = '''
entry:
  %src = call i32 @rk_reg(ptr %cpu, i32 6)
  %other = call i32 @rk_reg(ptr %cpu, i32 7)
  %dst = call i32 @rk_reg(ptr %cpu, i32 3)
  %v = call double @rk_load(ptr %cpu, i32 %src)
  call void @rk_push(ptr %cpu, double %v)
  call void @rk_test_ah(ptr %cpu, i32 1)
  %flag = call i32 @rk_zf(ptr %cpu, i32 0)
  %cond = icmp ne i32 %flag, 0
  br i1 %cond, label %left, label %right
left:
  %lv = call double @rk_load(ptr %cpu, i32 %other)
  call void @rk_push(ptr %cpu, double %lv)
  call void @rk_pop(ptr %cpu)
  %st = call double @rk_read(ptr %cpu, i32 0)
  %neg = fneg double %st
  call void @rk_set(ptr %cpu, i32 0, double %neg)
  br label %join
right:
  %addr = add i32 %other, 4
  %rv = call double @rk_load(ptr %cpu, i32 %addr)
  call void @rk_push(ptr %cpu, double %rv)
  call void @rk_pop(ptr %cpu)
  br label %join
join:
  call void @rk_observe(ptr %cpu)
  %result = call double @rk_read(ptr %cpu, i32 0)
  call void @rk_store(ptr %cpu, i32 %dst, double %result)
  call void @rk_pop(ptr %cpu)
  ret void
'''

# Same operations through current runtime primitives, with no stack lifting.
BASELINE = '''
extern void rk_observe(X86 *);
fpush(c, (double)rdf32(c->r[R_ESI]));
unsigned v = ((c->r[R_EAX] >> 8) & 255u) & 1u;
c->eflags_cf = c->eflags_of = 0;
c->eflags_zf = v == 0;
c->eflags_sf = (v >> 7) & 1u;
c->eflags_pf = parity8(v);
if (c->eflags_zf) {
    fpush(c, (double)rdf32(c->r[R_EDI]));
    fdrop(c);
    fset(c, 0, -ST(c, 0));
} else {
    fpush(c, (double)rdf32(c->r[R_EDI] + 4));
    fdrop(c);
}
rk_observe(c);
wrf32(c->r[R_EBX], fto_float(c, ST(c, 0)));
fdrop(c);
'''

# Unlike the real predicates, this leaves FNSTSW's TOP bits live in EAX.
# Returning with the original TOP must not change the earlier status result.
STATUS_BODY = """
entry:
  %src = call i32 @rk_reg(ptr %cpu, i32 6)
  %v = call double @rk_load(ptr %cpu, i32 %src)
  call void @rk_push(ptr %cpu, double %v)
  call void @rk_fnstsw(ptr %cpu)
  call void @rk_pop(ptr %cpu)
  ret void
"""
STATUS_BASELINE = """
fpush(c, (double)rdf32(c->r[R_ESI]));
rk_observe(c);
c->r[R_EAX] = (c->r[R_EAX] & 0xffff0000u) | fstsw(c);
fdrop(c);
"""
EXTRA_CASES = {'cfg_join': (BODY, BASELINE), 'status_top': (STATUS_BODY, STATUS_BASELINE)}

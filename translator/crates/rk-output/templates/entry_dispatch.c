
{signature}
{
    RecompSaved saved_; recomp_save(c, &saved_);
    if (recomp_profile_enabled) { recomp_call(c, recomp_func_addrs[i]); return; }
#ifndef RECOMP_NO_HOOKS
    if (__builtin_expect(__atomic_load_n(&recomp_hooked[i], __ATOMIC_ACQUIRE) != 0u, 0)) {
        /* A hook callback observes the full register file, so a pending lazy
         * descriptor must be materialised before it runs.  The callee's own
         * entry settle may have been elided. */
        x86_cc_settle(c);
        recomp_hook_ptrs[i](c, i);
    } else
#endif
{base_call}
#ifndef RECOMP_NO_HOOKS
    if (recomp_frame_watch)
        recomp_check_saved(c, recomp_func_addrs[i], &saved_);
#endif
}

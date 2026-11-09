# Demo timing. Terraform loads this file automatically.
#
# These are much shorter than the defaults so a viewer can watch the safety net
# work in a few minutes instead of half an hour. A real system would use longer
# waits so slow-but-working suppliers are never given up on too early.
# Delete this file to go back to the production-style defaults.
reconcile_schedule      = "rate(1 minute)"
stale_pending_seconds   = 30
stale_paid_seconds      = 30
give_up_seconds         = 180

#!/bin/sh
# Regenerate every card in dependency order. Map0Factory before Notify1Arrive (reads it); Map1Spec before the overlays (reads it).
set -e
cd "$(dirname "$0")"
for g in gen_bundle gen_tokens_type gen_factory gen_notify_a gen_notify_b gen_notify_c gen_graph build_overlays gen_part gen_agent gen_stage gen_grid8 gen_reviewer gen_plan \
         gen_inbox gen_queue gen_escalate gen_receipt gen_trust gen_spend gen_learning gen_health gen_setup gen_feature gen_settings \
         gen_c_seg gen_c_tabs gen_c_misc gen_c_surf gen_c_live gen_c_chrome gen_found; do
  if [ -f $g.py ]; then python3 $g.py > /dev/null || { echo "FAILED $g"; exit 1; }; else echo "missing $g"; fi
done
# last: the frames that share a moment are wired together (it reads the frames above, so nothing may run after it)
python3 wire_frames.py > /dev/null || { echo "FAILED wire_frames"; exit 1; }
# the prototype-only frames (screens drawn at 21:40 that are not cards), then the prototype, which joins every frame
# drawn at 21:40 into one page (it reads out/static, where wiring kept the cards as drawn)
python3 gen_proto_parts.py > /dev/null || { echo "FAILED gen_proto_parts"; exit 1; }
python3 gen_proto_step.py > /dev/null || { echo "FAILED gen_proto_step"; exit 1; }
python3 build_prototype.py > /dev/null || { echo "FAILED build_prototype"; exit 1; }
echo built

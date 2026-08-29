#!/bin/sh
set -eu

output_uid=$(stat -c '%u' /app/outputs)
output_gid=$(stat -c '%g' /app/outputs)

if [ "$output_uid" -eq 0 ]; then
    output_uid=1000
    output_gid=1000
    chown -R "$output_uid:$output_gid" /app/outputs
fi

runtime_home="/tmp/agentscroll-home-$output_uid"
mkdir -p "$runtime_home"
chown "$output_uid:$output_gid" "$runtime_home"
export HOME="$runtime_home"

exec gosu "$output_uid:$output_gid" agentscroll "$@"

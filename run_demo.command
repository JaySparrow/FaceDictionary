#!/bin/bash
# Open the demo vault (public LFW photos, password: demo-password). Your real vault is not touched.
cd "$(dirname "$0")"
FACEDICT_VAULT="$PWD/demo/vault.noindex" exec ./run.command "$@"

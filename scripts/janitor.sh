#!/bin/sh
# Deletes OpenShorts' files of jobs clipbot is done with (the janitor service in docker-compose.yml).
# clipbot can't do it itself: those files belong to OpenShorts' own user. clipbot drops an empty
# file named after the OpenShorts job id into data/purge/; this removes that job's work folder
# and source upload, then the request.
set -u
PURGE=${PURGE:-/purge}
OUTPUT=${OUTPUT:-/output}
UPLOADS=${UPLOADS:-/uploads}
ONCE=${ONCE:-}

trap 'exit 0' TERM INT

sweep() {
    for request in "$PURGE"/*; do
        [ -f "$request" ] || continue
        id=$(basename "$request")
        case "$id" in
            *[!0-9a-f-]* | -*)
                echo "janitor: ignoring request '$id' (not an OpenShorts job id)" ;;
            *)
                rm -rf "${OUTPUT:?}/$id"
                rm -f "${UPLOADS:?}/${id}"_*
                echo "janitor: deleted OpenShorts files of job $id" ;;
        esac
        rm -f "$request"
    done
}

while :; do
    sweep
    [ -n "$ONCE" ] && exit 0
    sleep 5 &
    wait $!
done

#!/bin/sh
# Install this launcher outside the versions folder. Never evaluate switch data.
set -eu
[ "$#" -ge 3 ] || exit 2
versions=$1
first=$2
shift 2
export ADT_VERSIONS_DIR="$versions"
current= previous= pending= pending_since= pending_starts= refused= notice=
valid=1
seen=' '
sha() {
    [ "${#1}" -eq 40 ] || return 1
    case $1 in *[!0-9a-f]*) return 1 ;; esac
}
number() {
    [ "${#1}" -le 12 ] || return 1
    case $1 in ''|*[!0-9]*) return 1 ;; esac
}
valid_notice() {
    [ -z "$1" ] && return 0
    # Field splitting here validates words only; no execution or glob expansion.
    given_notice=$1
    saved_ifs=$IFS
    IFS=' '
    set -f
    set -- $1
    IFS=$saved_ifs
    case $# in
        5) [ "$1 $2" = 'Upgraded from' ] && sha "$3" &&
           [ "$4" = to ] && sha "$5" &&
           [ "$given_notice" = "Upgraded from $3 to $5" ] && return 0 ;;
        7) [ "$1 $2 $3" = 'Rolled back from' ] && sha "${4%:}" &&
           [ "$4" = "${4%:}:" ] && [ "$5 $6 $7" = 'did not confirm' ] &&
           [ "$given_notice" = "Rolled back from $4 did not confirm" ] && return 0 ;;
    esac
    return 1
}
if [ -e "$versions/switch" ] || [ -L "$versions/switch" ]; then
    if [ -r "$versions/switch" ] && [ -f "$versions/switch" ]; then
        while IFS= read -r line || [ -n "$line" ]; do
            case $line in *=*) key=${line%%=*}; value=${line#*=} ;; *) valid=0; break ;; esac
            case $seen in *" $key "*) valid=0; break ;; esac
            seen="$seen$key "
            case $key in
                current|previous|pending)
                    if [ -n "$value" ] && ! sha "$value"; then valid=0; break; fi
                    case $key in current) current=$value ;; previous) previous=$value ;; pending) pending=$value ;; esac ;;
                pending_since|pending_starts)
                    if [ -n "$value" ] && ! number "$value"; then valid=0; break; fi
                    case $key in pending_since) pending_since=$value ;; pending_starts) pending_starts=$value ;; esac ;;
                refused)
                    refused=$value
                    rest=$value
                    while [ -n "$rest" ]; do
                        item=${rest%% *}
                        sha "$item" || { valid=0; break; }
                        case $rest in *' '*) rest=${rest#* }; [ -n "$rest" ] || valid=0 ;; *) rest= ;; esac
                    done ;;
                notice) notice=$value; valid_notice "$value" || valid=0 ;;
                *) valid=0; break ;;
            esac
        done < "$versions/switch"
        for key in current previous pending pending_since pending_starts refused notice; do
            case $seen in *" $key "*) ;; *) valid=0 ;; esac
        done
    else
        valid=0
    fi
fi
chosen=$first
if [ "$valid" -eq 1 ]; then
    [ -z "$current" ] || chosen=$versions/$current/install
    if [ -n "$pending" ]; then
        now=$(date +%s)
        # Strip leading zeros so POSIX arithmetic never interprets them as octal.
        starts=${pending_starts:-0}
        since=${pending_since:-0}
        while [ "${starts#0}" != "$starts" ]; do starts=${starts#0}; done
        while [ "${since#0}" != "$since" ]; do since=${since#0}; done
        starts=${starts:-0}
        since=${since:-0}
        if [ "$starts" -ge 3 ] || { [ "$since" -gt 0 ] && [ "$((now - since))" -ge 600 ]; }; then
            case " $refused " in *" $pending "*) ;; *) refused=${refused:+$refused }$pending ;; esac
            notice="Rolled back from $pending: did not confirm"
            pending= pending_since= pending_starts=
        else
            pending_starts=$((starts + 1))
            [ "$since" -ne 0 ] || since=$now
            pending_since=$since
            chosen=$versions/$pending/install
        fi
        umask 077
        temporary=$versions/.switch-launch-$$
        # noclobber prevents a stale file or symlink from being overwritten.
        (set -C; : > "$temporary") || exit 1
        {
            printf 'current=%s\nprevious=%s\npending=%s\npending_since=%s\npending_starts=%s\nrefused=%s\nnotice=%s\n' \
                "$current" "$previous" "$pending" "$pending_since" "$pending_starts" "$refused" "$notice"
        } > "$temporary"
        mv -f "$temporary" "$versions/switch"
    fi
fi
cd "$chosen"
exec "$@"

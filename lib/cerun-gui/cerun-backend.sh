#!/bin/bash
set -u

EXEC=""
STEAMPATH=""
APPID=""
COMPAT_PATH=""
PRTEXEC=""
RUNTIME_PID=""

die() { printf '%s\n' "$*" >&2; exit 1; }

expand_path() {
	REPLY=$1
	case "$REPLY" in
		'~/'*) REPLY="$HOME/${REPLY:2}" ;;
		'$HOME/'*) REPLY="$HOME/${REPLY:6}" ;;
	esac
}

# Each PID is visited once during the lookup.
declare -A PROCESS_ENV=()
read_environment() {
	local pid=$1 entry key
	[ -r "/proc/$pid/environ" ] || return 0
	while IFS= read -r -d '' entry; do
		key=${entry%%=*}
		case "$key" in
			STEAM_COMPAT_DATA_PATH|WINEPREFIX|SteamAppId|SteamGameId|STEAM_COMPAT_TOOL_PATHS)
				PROCESS_ENV[$pid:$key]=${entry#*=} ;;
		esac
	done < "/proc/$pid/environ"
}

accept_prefix() {
	local candidate=$1
	[[ -z "$COMPAT_PATH" ]] || return 0
	if [[ "$candidate" == \"*\" || "$candidate" == \'*\' ]]; then candidate=${candidate:1:${#candidate}-2}; fi
	expand_path "$candidate"
	candidate=${REPLY%/}
	[ "${candidate##*/}" != pfx ] || candidate=${candidate%/pfx}
	if [[ "$candidate" == /* ]] && [ -d "$candidate/pfx/drive_c" ] && [ -r "$candidate/pfx/user.reg" ]; then
		COMPAT_PATH=$candidate
	fi
}

# Keep the Proton order: selected environment, prefix metadata, launch ancestry.
proton_metadata() {
	local file candidate depth
	[ -z "$PRTEXEC" ] && [ -n "$COMPAT_PATH" ] || return 0
	for file in "$COMPAT_PATH/config_info" "$COMPAT_PATH/pfx/config_info"; do
		[ -r "$file" ] || continue
		while IFS= read -r candidate; do
			[[ "$candidate" == /* ]] || continue
			candidate=${candidate%/}
			for ((depth = 0; depth < 8; depth++)); do
				if [ -x "$candidate/proton" ]; then PRTEXEC="$candidate/proton"; return; fi
				candidate=${candidate%/*}
			done
		done < "$file"
	done
}

lookup_launch() {
	local pid=$SELECTED_PID parent field value arg rest match depth path
	local ancestry_proton="" assignment_re="(STEAM_COMPAT_DATA_PATH|WINEPREFIX)=(\"[^\"]*\"|'[^']*'|[^[:space:];]+)"
	local proton_re="(\"/[^\"]*/proton\"|'/[^']*/proton'|/[^[:space:]\"']*/proton)"
	local -a paths=()
	# Walk each PID once, retaining Proton candidates while seeking a prefix.
	for ((depth = 0; depth < 128; depth++)); do
		[ -z "$COMPAT_PATH" ] || [ -z "$PRTEXEC" ] || break
		[[ "$pid" =~ ^[0-9]+$ ]] && ((pid > 1)) || break
		read_environment "$pid"
		APPID=${APPID:-${PROCESS_ENV[$pid:SteamAppId]:-${PROCESS_ENV[$pid:SteamGameId]-}}}
		if [ -z "$PRTEXEC" ]; then
			IFS=: read -r -a paths <<< "${PROCESS_ENV[$pid:STEAM_COMPAT_TOOL_PATHS]-}"
			for path in "${paths[@]}"; do
				if [ -x "$path/proton" ]; then PRTEXEC="$path/proton"; break; fi
			done
		fi
		accept_prefix "${PROCESS_ENV[$pid:STEAM_COMPAT_DATA_PATH]-}"
		accept_prefix "${PROCESS_ENV[$pid:WINEPREFIX]-}"
		if [ -r "/proc/$pid/cmdline" ]; then
			while IFS= read -r -d '' arg; do
				case "$arg" in
					STEAM_COMPAT_DATA_PATH=*|WINEPREFIX=*|--env=STEAM_COMPAT_DATA_PATH=*|--env=WINEPREFIX=*)
						value=${arg#--env=}; accept_prefix "${value#*=}" ;;
				esac
				rest=$arg
				while [[ "$rest" =~ $assignment_re ]]; do
					match=${BASH_REMATCH[0]}; value=${BASH_REMATCH[2]}
					accept_prefix "$value"; rest=${rest#*"$match"}
				done
				if [ -z "$ancestry_proton" ]; then
					value=$arg
					if [[ "$arg" != /*/proton ]] && [[ "$arg" =~ $proton_re ]]; then
						value=${BASH_REMATCH[1]}
						if [[ "$value" == \"*\" || "$value" == \'*\' ]]; then value=${value:1:${#value}-2}; fi
					fi
					if [[ "$value" == /*/proton ]] && [ -x "$value" ]; then ancestry_proton=$value; fi
				fi
			done < "/proc/$pid/cmdline"
		fi
		proton_metadata
		if [ -n "$COMPAT_PATH" ] && [ -z "$PRTEXEC" ]; then PRTEXEC=$ancestry_proton; fi
		parent=""
		if [ -r "/proc/$pid/status" ]; then
			while read -r field value rest; do
				if [ "$field" = PPid: ]; then parent=$value; break; fi
			done < "/proc/$pid/status"
		fi
		[ "$parent" != "$pid" ] || break
		pid=$parent
	done
	if [ -z "$COMPAT_PATH" ] && [ -n "$APPID" ]; then
		paths=("$STEAMPATH")
		if [ -r "$STEAMPATH/steamapps/libraryfolders.vdf" ]; then
			while IFS= read -r path; do paths+=("$path"); done < <(
				sed -n 's/.*"path"[[:space:]]*"\(.*\)"/\1/p' "$STEAMPATH/steamapps/libraryfolders.vdf" | sed 's#\\\\#/#g'
			)
		fi
		for path in "${paths[@]}"; do accept_prefix "$path/steamapps/compatdata/$APPID"; done
	fi
	[ -n "$COMPAT_PATH" ] || die "No usable prefix found in the process, launch ancestry, or Steam libraries."
	proton_metadata
	PRTEXEC=${PRTEXEC:-$ancestry_proton}
	[ -x "$PRTEXEC" ] || die "Found prefix $COMPAT_PATH, but could not determine its Proton executable."
	PRTEXEC=$(readlink -f -- "$PRTEXEC")
}

prepare_runtime() {
	RUNTIME_PID=""
	# A Flatpak game can share prefix files with the host but use a different
	# /tmp Wine socket and PID namespace. Join its exact runtime, not a new one.
	if [ -f "/proc/$SELECTED_PID/root/.flatpak-info" ] &&
		! [ "/proc/$SELECTED_PID/ns/mnt" -ef /proc/self/ns/mnt ]; then
		command -v flatpak >/dev/null 2>&1 || die "The selected process needs flatpak enter, but flatpak was not found."
		RUNTIME_PID=$SELECTED_PID
		echo "Using the selected process's Flatpak runtime (PID $RUNTIME_PID)."
	fi
}

run_proton() {
	# flatpak enter copies the target's environment. These Wine values describe
	# the existing process, including an FD that is not inherited by this one.
	local -a command=(/usr/bin/env -u WINESERVERSOCKET -u WINELOADERNOEXEC -u WINEPRELOADRESERVE
		"STEAM_COMPAT_DATA_PATH=$COMPAT_PATH" "STEAM_COMPAT_CLIENT_INSTALL_PATH=$STEAMPATH"
		"$PRTEXEC" runinprefix "$@")
	if [ -n "$RUNTIME_PID" ]; then
		command=(flatpak enter "$RUNTIME_PID" "${command[@]}")
	fi
	printf 'Running command: '; printf '%q ' "${command[@]}"; printf '\n'
	"${command[@]}"
}

# One definition feeds both the saved-registry comparison and the import.
# LastProcessListDownload belongs to CE's cache management; leave it alone.
registry_data() {
	printf 'Windows Registry Editor Version 5.00\n\n'
	case "$APP_KIND" in
		ce) cat <<'REG'
[HKEY_CURRENT_USER\Software\Cheat Engine]
"Addresslist: sort on click"=dword:00000000
"DPI Aware"=dword:00000001
"First Time User"=dword:00000000

[HKEY_CURRENT_USER\Software\Cheat Engine\ceshare]
"enabled"="1"
REG
			;;
		aurora) printf '[HKEY_CURRENT_USER\\Software\\Wine\\AppDefaults\\Aurora.exe\\X11 Driver]\n"Decorated"="%s"\n' "$AURORA_DECORATED" ;;
	esac
}

registry_matches() {
	[ -r "$COMPAT_PATH/pfx/user.reg" ] || return 1
	# Wine may not have flushed live changes yet. Compare the saved values;
	# no Wine/Proton process or Python installation is needed for this check.
	awk '
		/^\[/ {
			section=tolower($0); sub(/\].*$/, "", section); sub(/^\[/, "", section)
			sub(/^hkey_current_user\\/, "", section)
			gsub(/\\\\/, "\\", section); next
		}
		/^"[^"\\]*"=/ {
			name=$0; sub(/=.*/, "", name)
			value=substr($0, length(name)+2)
			key=section SUBSEP tolower(name)
			if (NR==FNR) expected[key]=tolower(value)
			else if (key in expected) actual[key]=tolower(value)
		}
		END {
			for (key in expected) if (!(key in actual) || actual[key]!=expected[key]) exit 1
			if (!length(expected)) exit 1
		}
	' <(registry_data) "$COMPAT_PATH/pfx/user.reg"
}

apply_registry() (
	local temp
	temp=$(mktemp "$COMPAT_PATH/pfx/drive_c/cerun-settings.XXXXXX.reg") || exit 1
	trap 'rm -f -- "$temp"' EXIT
	registry_data > "$temp" || exit 1
	run_proton reg.exe import "C:\\${temp##*/}"
)

main() {
	local status basename
	[ "$#" -eq 4 ] && [ "$1" = --pid ] && [[ "$2" =~ ^[0-9]{1,9}$ ]] &&
		((10#$2 > 1)) || die "Usage: cerun-backend.sh --pid PID /path/to/tool.exe /path/to/steam"
	SELECTED_PID=$((10#$2))
	EXEC=$3
	STEAMPATH=$4
	local logfile="/tmp/cerun.$(date '+%Y%m%d-%H%M%S-%N').log"
	exec > >(tee -a "$logfile") 2>&1
	echo "cerun log: $logfile"
	[ -f "$EXEC" ] || die "Executable not found: $EXEC"
	[[ "$STEAMPATH" == /* ]] && [ -d "$STEAMPATH/steamapps" ] || die "Steam installation not found: $STEAMPATH"
	[ -r "/proc/$SELECTED_PID/status" ] || die "Selected process $SELECTED_PID is no longer available. Refresh the process list."
	echo "Selected process: PID $SELECTED_PID"
	lookup_launch
	prepare_runtime
	printf 'AppID: %s\nSteam installation: %s\nUsing Proton: %s\nCompat data path: %s\nWine prefix: %s\nSelected executable: %s\n' \
		"${APPID:-not available}" "$STEAMPATH" "$PRTEXEC" "$COMPAT_PATH" "$COMPAT_PATH/pfx" "$EXEC"
	basename=${EXEC##*/}
	APP_KIND=""
	case "${basename,,}" in
		cheatengine*.exe|"cheat engine"*.exe) APP_KIND=ce ;;
		aurora.exe)
			APP_KIND=aurora; AURORA_DECORATED=${CERUN_AURORA_DECORATED:-N}
			[[ "$AURORA_DECORATED" == [YN] ]] || die "CERUN_AURORA_DECORATED must be Y or N." ;;
	esac
	if [ -n "$APP_KIND" ]; then
		if registry_matches; then echo "Application settings already match; skipping registry setup."
		else
			echo "Applying application settings..."
			apply_registry || die "Could not apply settings; executable was not launched."
		fi
	fi
	echo "Launching..."
	run_proton "$EXEC"
	status=$?
	echo "Proton process exited with status $status."
	return "$status"
}

# Keep helpers sourceable for isolated regression checks without launching apps.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then main "$@"; fi

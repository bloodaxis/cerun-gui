#!/bin/bash
# Usage: cerun-configured [automatic [1-3] | ls | /path/to/tool.exe]
# GUI/noninteractive: cerun-configured --pid PID /path/to/tool.exe
# No arguments opens the launch/configuration menu. Config values are plain
# paths, not shell code. Registry changes apply only to the selected helper.
set -u

CONFIG_DIR="$HOME/.config/cerun"
CONFIG_FILE="$CONFIG_DIR/executables.conf"
CONFIG_KEYS=(DEFAULT_EXE EXE_1 EXE_2 EXE_3 STEAMPATH)
for key in "${CONFIG_KEYS[@]}"; do printf -v "$key" ''; done
AUTOMATIC=false
HEADLESS=false
EXEC=""
APPID=""
COMPAT_PATH=""
PRTEXEC=""
RUNTIME_PID=""

die() { printf '%s\n' "$*" >&2; exit 1; }

# REPLY is shared by input helpers, avoiding a subshell for every prompt/path.
expand_path() {
	REPLY=$1
	case "$REPLY" in
		'~/'*) REPLY="$HOME/${REPLY:2}" ;;
		'$HOME/'*) REPLY="$HOME/${REPLY:6}" ;;
	esac
}

choose() {
	local answer
	while true; do
		read -rp "$1: " answer < /dev/tty || return 1
		# Limit length before arithmetic; use decimal even for leading zeros.
		if [[ "$answer" =~ ^[0-9]{1,9}$ ]] &&
			((10#$answer >= $2 && 10#$answer <= $3)); then
			REPLY=$((10#$answer)); return 0
		fi
		echo "Invalid selection."
	done
}

prompt_path() {
	local target=$1 value
	"$HEADLESS" && die "Configure $target in $CONFIG_FILE before launching without a terminal."
	echo "Enter $target without quotes (~/ is accepted)."
	case "$target" in
		STEAMPATH) echo "Use the Steam installation folder containing steamapps." ;;
		EXE_[1-3]) echo "Press Enter to clear this optional shortcut." ;;
	esac
	while true; do
		read -rp "$target: " value < /dev/tty || return 1
		expand_path "$value"
		if [[ "$target" == EXE_[1-3] && -z "$REPLY" ]]; then return 0; fi
		if [[ "$REPLY" == /* ]]; then
			if [ "$target" = STEAMPATH ]; then
				if [ -d "$REPLY/steamapps" ]; then REPLY=${REPLY%/}; return 0; fi
			elif [ -f "$REPLY" ]; then return 0
			fi
		fi
		echo "Path is not usable. Enter an existing absolute path."
	done
}

save_config() (
	local temp key
	mkdir -p -- "$CONFIG_DIR" || exit 1
	temp=$(mktemp "$CONFIG_DIR/.executables.XXXXXX") || exit 1
	trap 'rm -f -- "$temp"' EXIT
	{
		printf '# Executable and Steam paths: plain values, no shell quotes.\n'
		for key in "${CONFIG_KEYS[@]}"; do printf '%s=%s\n' "$key" "${!key}"; done
	} > "$temp" && mv -- "$temp" "$CONFIG_FILE" || exit 1
	echo "Saved paths to $CONFIG_FILE"
)

load_config() {
	local line key
	if [ ! -f "$CONFIG_FILE" ]; then
		"$HEADLESS" && die "Config not found: $CONFIG_FILE. Save paths in the GUI first."
		echo "No config found. Set the default executable, optional shortcuts, and Steam path."
		for key in "${CONFIG_KEYS[@]}"; do
			prompt_path "$key" || die "Setup cancelled; no config saved."
			printf -v "$key" '%s' "$REPLY"
		done
		save_config || die "Could not save $CONFIG_FILE."
		return
	fi
	[ -r "$CONFIG_FILE" ] || die "Cannot read $CONFIG_FILE."
	while IFS= read -r line || [ -n "$line" ]; do
		case "$line" in ''|'#'*) continue ;; esac
		[[ "$line" == *=* ]] || die "Invalid config line: $line"
		key=${line%%=*}
		case "$key" in
			DEFAULT_EXE|EXE_[1-3]|STEAMPATH)
				expand_path "${line#*=}"; printf -v "$key" '%s' "$REPLY" ;;
			*) die "Unknown config key: $key" ;;
		esac
	done < "$CONFIG_FILE"
	[ -n "$DEFAULT_EXE" ] || die "Set DEFAULT_EXE in $CONFIG_FILE."
	if [[ "$STEAMPATH" != /* ]] || [ ! -d "$STEAMPATH/steamapps" ]; then
		prompt_path STEAMPATH || die "Setup cancelled; Steam path was not saved."
		STEAMPATH=$REPLY
		save_config || die "Could not save $CONFIG_FILE."
	fi
}

list_paths() {
	local i key
	for ((i = 0; i < $1; i++)); do
		key=${CONFIG_KEYS[i]}
		printf ' %d) %-11s %s\n' "$((i + 1))" "$key" "${!key:-not configured}"
	done
}

edit_config() {
	local key old
	list_paths 5
	echo " 0) Back to launch menu"
	choose 'Select path to update [0-5]' 0 5 || { echo 'Configuration cancelled.'; return 0; }
	((REPLY)) || return 0
	key=${CONFIG_KEYS[REPLY - 1]}
	prompt_path "$key" || { echo "Configuration cancelled; no changes saved."; return 0; }
	old=${!key}
	printf -v "$key" '%s' "$REPLY"
	if ! save_config; then
		printf -v "$key" '%s' "$old"
		echo "Could not save config; previous value retained."
	fi
}

select_executable() {
	list_paths 4
	choose 'Select executable [1-4]' 1 4 || die "Launch cancelled."
	local key=${CONFIG_KEYS[REPLY - 1]}
	EXEC=${!key}
}

launch_menu() {
	while true; do
		cat <<'USAGE'
Command-line usage:
 cerun-configured                 Show this menu
 cerun-configured automatic       Automatically launch the default executable
 cerun-configured automatic 1-3   Automatically launch EXE_1, EXE_2, or EXE_3
 cerun-configured ls              Select an executable, then a running process
 cerun-configured "/path/tool.exe" Use a custom executable, then select a process
 Replace 1-3 with one number. Quote command-line paths containing spaces.

Launch mode:
 1) Automatic: default executable in the running Steam game
 2) Select executable, then select the running process/prefix
 3) Enter an executable path, then select the running process/prefix
 4) Rerun configuration (choose one of five saved paths)
USAGE
		# Keep the menu's existing automatic/ls aliases as well as its numbers.
		read -rp 'Select launch mode [1-4]: ' REPLY < /dev/tty || die "Launch cancelled."
		case "$REPLY" in
			1|automatic) AUTOMATIC=true; EXEC=$DEFAULT_EXE; return ;;
			2|ls) select_executable; return ;;
			3) prompt_path EXEC || die "Launch cancelled."; EXEC=$REPLY; return ;;
			4) edit_config ;;
			*) echo "Invalid selection." ;;
		esac
	done
}

select_process() {
	local pid command_line name id i
	local -a pids=() names=() commands=()
	local -A steam_pids=()
	# Read comm separately: its spaces must not shift the command-line column.
	while read -r pid command_line; do
		if "$AUTOMATIC"; then
			if [[ "$command_line" =~ (^|[[:space:]])AppId=([0-9]+)($|[[:space:]]) ]]; then
				id=${BASH_REMATCH[2]}
				[ "$id" = 0 ] || steam_pids[$id]=$pid
			fi
		else
			name=""
			if [ -r "/proc/$pid/comm" ]; then IFS= read -r name < "/proc/$pid/comm" || :; fi
			if [[ "${name,,}" == *.exe || "${command_line,,}" == *'.exe'* ]]; then
				pids+=("$pid"); names+=("$name"); commands+=("$command_line")
			fi
		fi
	done < <(ps -eo pid=,args=)
	if "$AUTOMATIC"; then
		[ "${#steam_pids[@]}" -eq 1 ] || die "automatic requires exactly one running Steam game; found ${#steam_pids[@]}. Use ls to select a process."
		for APPID in "${!steam_pids[@]}"; do SELECTED_PID=${steam_pids[$APPID]}; done
		echo "Automatically selected Steam AppID: $APPID"
	else
		[ "${#pids[@]}" -gt 0 ] || die "No running .exe processes detected."
		for i in "${!pids[@]}"; do
			printf '%2d) PID %-7s %-28s %s\n' "$((i + 1))" "${pids[i]}" "${names[i]}" "${commands[i]:0:100}"
		done
		choose "Select process [1-${#pids[@]}]" 1 "${#pids[@]}" || die "Launch cancelled."
		SELECTED_PID=${pids[REPLY - 1]}
		echo "Selected process: ${names[REPLY - 1]} (PID $SELECTED_PID)"
	fi
}

# Cache each environment once, including during fallback traversal.
declare -A PROCESS_ENV=() ENV_LOADED=()
read_environment() {
	local pid=$1 entry key
	[ "${ENV_LOADED[$pid]-}" ] && return
	ENV_LOADED[$pid]=1
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
	read_environment "$pid"
	APPID=${APPID:-${PROCESS_ENV[$pid:SteamAppId]:-${PROCESS_ENV[$pid:SteamGameId]-}}}
	IFS=: read -r -a paths <<< "${PROCESS_ENV[$pid:STEAM_COMPAT_TOOL_PATHS]-}"
	for path in "${paths[@]}"; do
		if [ -x "$path/proton" ]; then PRTEXEC="$path/proton"; break; fi
	done
	accept_prefix "${PROCESS_ENV[$pid:STEAM_COMPAT_DATA_PATH]-}"
	accept_prefix "${PROCESS_ENV[$pid:WINEPREFIX]-}"
	proton_metadata
	# Walk once only when necessary, retaining Proton candidates while seeking a prefix.
	for ((depth = 0; depth < 128; depth++)); do
		[ -z "$COMPAT_PATH" ] || [ -z "$PRTEXEC" ] || break
		[[ "$pid" =~ ^[0-9]+$ ]] && ((pid > 1)) || break
		read_environment "$pid"
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
		flatpak enter "$RUNTIME_PID" "${command[@]}"
	else
		"${command[@]}"
	fi
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
	local mode=${1-} key status basename
	case "$mode" in
		--pid)
			[ "$#" -eq 3 ] && [[ "$2" =~ ^[0-9]{1,9}$ ]] && ((10#$2 > 1)) || die "Usage: cerun-configured --pid PID /path/to/tool.exe"
			HEADLESS=true; SELECTED_PID=$((10#$2)) ;;
		automatic)
			[ "$#" -le 2 ] && { [ "$#" -eq 1 ] || [[ "$2" == [1-3] ]]; } || die "Usage: cerun-configured automatic [1-3]"
			AUTOMATIC=true ;;
		*) [ "$#" -le 1 ] && [[ ! "$mode" =~ ^[0-9]+$ ]] || die "Use automatic [1-3], ls, or an executable path." ;;
	esac
	if ! "$HEADLESS" && { [ ! -t 0 ] || [ ! -t 1 ]; }; then
		local -a command=(bash -lc '"$@"; status=$?; printf "\nPress any key to exit..."; read -r -n 1 -s < /dev/tty; printf "\n"; exit "$status"' bash "$(readlink -f -- "$0")" "$@")
		if command -v xdg-terminal-exec >/dev/null 2>&1; then exec xdg-terminal-exec -- "${command[@]}"; fi
		if command -v x-terminal-emulator >/dev/null 2>&1; then exec x-terminal-emulator -e "${command[@]}"; fi
		die "No default-terminal launcher found. Run this script from a terminal."
	fi
	local logfile="/tmp/cerun.$(date '+%Y%m%d-%H%M%S-%N').log"
	exec > >(tee -a "$logfile") 2>&1
	echo "cerun log: $logfile"
	load_config
	case "$mode" in
		--pid) EXEC=$3 ;;
		'') launch_menu ;;
		automatic) key=DEFAULT_EXE; [ "$#" -eq 1 ] || key=EXE_$2; EXEC=${!key} ;;
		ls) select_executable ;;
		*) EXEC=$mode ;;
	esac
	[ -f "$EXEC" ] || die "Executable not found or shortcut not configured: $EXEC"
	if "$HEADLESS"; then
		[ -r "/proc/$SELECTED_PID/status" ] || die "Selected process $SELECTED_PID is no longer available. Refresh the process list."
		echo "Selected process: PID $SELECTED_PID"
	else
		select_process
	fi
	lookup_launch
	prepare_runtime
	printf 'AppID: %s\nUsing Proton: %s\nUsing prefix: %s\nSelected executable: %s\n' "${APPID:-not available}" "$PRTEXEC" "$COMPAT_PATH" "$EXEC"
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

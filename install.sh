#!/usr/bin/env bash
# linux-tg-yt-proxy: YouTube and Telegram for a home LAN without a VPN.
# Installs two services, picks the settings that work best on this network, checks everything.
#   curl -fsSL https://raw.githubusercontent.com/Aut0iq/linux-tg-yt-proxy/main/install.sh | sudo bash
# Re-run it any time to re-tune; ports and the Telegram secret are kept.
set -euo pipefail

REPO=Aut0iq/linux-tg-yt-proxy
TG_WS_PROXY_REF=${TG_WS_PROXY_REF:-307a384ab7f522e34bb1a045884d0bd90f40bee5}  # tested Flowseal/tg-ws-proxy commit
DIR=/opt/linux-tg-yt-proxy
CONF=/etc/linux-tg-yt-proxy.conf
MARK='# linux-tg-yt-proxy'

say()  { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "run as root: sudo bash install.sh"
command -v apt-get >/dev/null || die "only Debian/Ubuntu (apt) are supported"
command -v systemctl >/dev/null || die "systemd is required"

# Piped from curl: fetch the whole repo, then run that copy.
SRC=$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)
if [ ! -f "$SRC/yt-proxy.py" ]; then
  tmp=$(mktemp -d); auth=()
  [ -n "${GH_TOKEN:-}" ] && auth=(-H "Authorization: Bearer $GH_TOKEN")  # only while the repo is private
  curl -fsSL "${auth[@]}" "https://api.github.com/repos/$REPO/tarball/${REF:-main}" |
    tar xz -C "$tmp" --strip-components=1 || die "could not download $REPO"
  exec bash "$tmp/install.sh" "$@" </dev/null
fi

say "Installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends python3 python3-venv curl ca-certificates iproute2 openssl >/dev/null

# keep ports and the Telegram secret on re-runs, so clients keep working
if [ -f "$CONF" ]; then . "$CONF"; fi
YT_PORT=${YT_PORT:-1080}
TG_PORT=${TG_PORT:-1443}
TG_SECRET=${TG_SECRET:-$(openssl rand -hex 16)}
[[ $TG_SECRET =~ ^[0-9a-f]{32}$ ]] || die "TG_SECRET must be 32 hex chars"

say "Installing to $DIR (tg-ws-proxy ${TG_WS_PROXY_REF:0:7})"
systemctl stop yt-proxy tg-ws-proxy 2>/dev/null || true   # measure the network without our own traffic
mkdir -p "$DIR"
install -m 755 "$SRC/yt-proxy.py" "$SRC/tg-check.py" "$DIR/"
rm -rf "$DIR/tg-ws-proxy" && mkdir "$DIR/tg-ws-proxy"
curl -fsSL "https://codeload.github.com/Flowseal/tg-ws-proxy/tar.gz/$TG_WS_PROXY_REF" |
  tar xz -C "$DIR/tg-ws-proxy" --strip-components=1 || die "could not download Flowseal/tg-ws-proxy"
[ -x "$DIR/venv/bin/python" ] || python3 -m venv "$DIR/venv"
"$DIR/venv/bin/pip" install -q --disable-pip-version-check cryptography==46.0.5 certifi 'httpx[http2]==0.28.1'

# score N URL [curl args] -> how many of N requests got any HTTP answer
score() {
  local n=$1 url=$2 ok=0 code; shift 2
  for _ in $(seq "$n"); do
    code=$(curl -so /dev/null --connect-timeout 4 -m 7 -w '%{http_code}' "$@" "$url" || true)
    [ "$code" != 000 ] && ok=$((ok + 1))
  done
  echo "$ok"
}
ips() { getent ahostsv4 "$1" | awk '!s[$1]++ {print $1}' || true; }
wait_port() { for _ in $(seq 100); do (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null && return; sleep 0.1; done; }
yt_score() {  # proxy -> successes out of 12
  local ok=0 u
  for u in https://www.youtube.com/ https://i.ytimg.com/generate_204 https://redirector.googlevideo.com/generate_204; do
    ok=$((ok + $(score 4 "$u" -x "$1")))
  done
  echo "$ok"
}

say "Choosing the YouTube method (none / split / disorder)"
best=0; YT_STRATEGY=disorder
for s in none split disorder; do
  YT_PORT=18080 YT_STRATEGY=$s python3 "$DIR/yt-proxy.py" >/dev/null & pid=$!
  wait_port 18080
  ok=$(yt_score http://127.0.0.1:18080)
  kill "$pid"; wait "$pid" 2>/dev/null || true
  echo "    $s: $ok/12"
  if [ "$ok" -gt "$best" ]; then best=$ok; YT_STRATEGY=$s; fi
  [ "$ok" = 12 ] && break   # simplest method that always works wins
done
[ "$best" -gt 0 ] || warn "YouTube did not answer with any method, keeping 'disorder'"
echo "    -> $YT_STRATEGY"

say "Choosing Telegram server addresses (DC1-DC5)"
TG_ARGS=""
for dc in 1 2 3 4 5; do
  h=kws$dc.web.telegram.org; best=0; bip=""
  for ip in $( { ips "$h"; echo 149.154.167.220; } | awk 'NF && !s[$0]++'); do
    ok=$(score 5 "https://$h/apiws" --resolve "$h:443:$ip")
    if [ "$ok" -gt "$best" ]; then best=$ok; bip=$ip; fi
  done
  if [ "$best" -ge 3 ]; then TG_ARGS+=" --dc-ip $dc:$bip"; echo "    DC$dc: $bip ($best/5)"
  else echo "    DC$dc: blocked, goes through Cloudflare"; fi
done

say "Checking the Bot API address (api.telegram.org)"
tpl=$(ls /etc/cloud/templates/hosts.*.tmpl 2>/dev/null || true)  # cloud-init rebuilds /etc/hosts from these
for f in /etc/hosts $tpl; do sed -i "/$MARK\$/d" "$f"; done
dns=$(ips api.telegram.org)
best=-1; bip=""
for ip in $( { echo "$dns"; echo 149.154.167.220; } | awk 'NF && !s[$0]++'); do
  ok=$(score 6 https://api.telegram.org/ --resolve "api.telegram.org:443:$ip")
  echo "    $ip: $ok/6"
  if [ "$ok" -gt "$best" ]; then best=$ok; bip=$ip; fi
done
if [ "$best" -gt 0 ] && ! grep -qx "$bip" <<<"$dns"; then
  for f in /etc/hosts $tpl; do echo "$bip api.telegram.org  $MARK" >>"$f"; done
  echo "    -> pinned api.telegram.org to $bip in /etc/hosts"
fi

say "Starting services"
cat >"$CONF" <<EOF
# written by linux-tg-yt-proxy install.sh; re-run it to re-tune
YT_PORT=$YT_PORT
YT_STRATEGY=$YT_STRATEGY
TG_PORT=$TG_PORT
TG_SECRET=$TG_SECRET
TG_ARGS="${TG_ARGS# }"
EOF
chmod 600 "$CONF"
unit() {  # name, description, command
  cat >"/etc/systemd/system/$1.service" <<EOF
[Unit]
Description=$2
After=network-online.target
Wants=network-online.target

[Service]
EnvironmentFile=$CONF
WorkingDirectory=$DIR/tg-ws-proxy
ExecStart=$3
DynamicUser=yes
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
}
unit yt-proxy "YouTube DPI bypass proxy (linux-tg-yt-proxy)" "/usr/bin/python3 -u $DIR/yt-proxy.py"
unit tg-ws-proxy "Telegram MTProto proxy over WebSocket (linux-tg-yt-proxy)" \
  "$DIR/venv/bin/python -u proxy/tg_ws_proxy.py --host 0.0.0.0 --port \${TG_PORT} --secret \${TG_SECRET} \$TG_ARGS"
systemctl daemon-reload
systemctl enable -q yt-proxy tg-ws-proxy
systemctl restart yt-proxy tg-ws-proxy
wait_port "$YT_PORT"; wait_port "$TG_PORT"; sleep 2

say "Final check"
IP=$(ip -4 route get 1.1.1.1 | awk '{for (i = 1; i < NF; i++) if ($i == "src") print $(i + 1)}')
mark() { if [ "$1" = "$2" ]; then echo OK; elif [ "$1" -gt 0 ]; then echo WARN; else echo FAIL; fi; }
yt=$(yt_score "http://$IP:$YT_PORT")
echo "    YouTube through $IP:$YT_PORT: $yt/12 $(mark "$yt" 12)"
bot=$(score 10 https://api.telegram.org/ -x "http://127.0.0.1:$YT_PORT")
echo "    Bot API through http://127.0.0.1:$YT_PORT: $bot/10 $(mark "$bot" 10)"
fail=0; [ "$yt" -gt 0 ] && [ "$bot" -gt 0 ] || fail=1
echo "    Telegram proxy, every DC:"
"$DIR/venv/bin/python" "$DIR/tg-check.py" 127.0.0.1 "$TG_PORT" "$TG_SECRET" || fail=1
for s in yt-proxy tg-ws-proxy; do systemctl is-active -q "$s" || { fail=1; warn "$s is not running"; }; done

cat <<EOF

  Telegram (phone or PC, open the link):
    tg://proxy?server=$IP&port=$TG_PORT&secret=dd$TG_SECRET
  YouTube, proxy auto-config (PAC) on the device:
    http://$IP:$YT_PORT/proxy.pac
  or manual proxy, HTTP or SOCKS5: $IP:$YT_PORT
  Telegram bots on this server: proxy http://127.0.0.1:$YT_PORT, long polling.

EOF
[ "$fail" = 0 ] || die "some checks failed, see above"
say "Done"

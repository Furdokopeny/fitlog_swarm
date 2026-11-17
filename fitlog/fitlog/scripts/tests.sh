#!/usr/bin/env bash
# Kötelező tesztek a Swarm stackhez – manager node-on futtassa.
# Használat: ./scripts/tests.sh [stack_név] [alap_url]
set -u
STACK=${1:-fitlog}
URL=${2:-http://localhost:8080}
MARK="teszt-$(date +%s)"

step() { printf '\n=== %s ===\n' "$*"; }

wait_ready() {   # $1: service rövid neve; megvárja, míg minden replika fut
  local svc="${STACK}_$1" r
  for _ in $(seq 1 60); do
    r=$(docker service ls --filter "name=$svc" --format '{{.Name}} {{.Replicas}}' | awk -v n="$svc" '$1==n{print $2}')
    [ -n "$r" ] && [ "${r%%/*}" = "${r##*/}" ] && return 0
    sleep 2
  done
  echo "Időtúllépés: a(z) $svc nem állt helyre"; return 1
}

step "1. Funkcionális teszt: adat rögzítése és visszaolvasása"
curl -fsS -X POST "$URL/api/workouts" -H 'Content-Type: application/json' \
  -d "{\"exercise\":\"$MARK\",\"sets\":4,\"reps\":8,\"weight_kg\":60,\"duration_min\":45}"; echo
curl -fsS "$URL/api/workouts" | grep -q "$MARK" \
  && echo "OK: a bejegyzés ($MARK) visszaolvasható" || echo "HIBA: a bejegyzés nem található"

step "2. Skálázás: backend 2 -> 3 replika"
docker service scale "${STACK}_backend=3"
wait_ready backend
docker service ls --filter "name=${STACK}_backend"
docker service ps "${STACK}_backend" --filter desired-state=running
echo "Terheléselosztás (12 kérés, kiszolgáló task azonosítók):"
for _ in $(seq 1 12); do curl -fsS "$URL/api/health" | grep -oE '"host": ?"[^"]*"'; done | sort | uniq -c

step "3. Hibatűrés: egy backend konténer leállítása"
CID=$(docker ps --filter "name=${STACK}_backend" --format '{{.ID}}' | head -n1)
[ -z "$CID" ] && { echo "Nincs backend konténer ezen a node-on"; exit 1; }
echo "Leállított konténer: $CID"
docker kill "$CID" > /dev/null
sleep 3
wait_ready backend
docker service ps "${STACK}_backend" --format 'table {{.Name}}\t{{.CurrentState}}\t{{.Error}}'
curl -fsS "$URL/api/info"; echo

step "4. Perzisztencia: az adatbázis-konténer újralétrehozása"
echo "Régi db konténer: $(docker ps --filter "name=${STACK}_db" --format '{{.ID}}')"
docker service update --force "${STACK}_db" > /dev/null
wait_ready db
ok=0
for _ in $(seq 1 30); do
  if curl -fsS "$URL/api/workouts" 2>/dev/null | grep -q "$MARK"; then ok=1; break; fi
  sleep 2
done
echo "Új db konténer: $(docker ps --filter "name=${STACK}_db" --format '{{.ID}}')"
[ "$ok" = 1 ] && echo "OK: az adatok megmaradtak ($MARK)" || echo "HIBA: az adatok elvesztek"

step "Visszaállítás: backend 3 -> 2 replika"
docker service scale "${STACK}_backend=2"
docker stack services "$STACK"

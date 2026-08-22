#!/usr/bin/env bash
# =============================================================================
# Picox — scansione dei segreti sui file tracciati da git
# =============================================================================
# Gira in CI su ogni push e può essere lanciato in locale prima di committare:
#
#     bash .github/scripts/scan-secrets.sh
#
# Analizza solo ciò che git traccia (`git ls-files`): `node_modules`, `.next`,
# `.venv` e i file ignorati restano fuori senza bisogno di elencarli, e
# soprattutto non si scansiona roba che non finirà mai in un commit.
#
# Nota su cosa NON fa: guarda l'albero corrente, non lo storico. Un segreto
# committato e poi rimosso resta nella cronologia e va revocato, non
# "cancellato" — per quello serve uno scanner sullo storico (gitleaks,
# trufflehog) o `git filter-repo`.
set -uo pipefail

cd "$(git rev-parse --show-toplevel)"

ROSSO=$'\033[31m'; VERDE=$'\033[32m'; GIALLO=$'\033[33m'; RESET=$'\033[0m'
[[ -t 1 ]] || { ROSSO=""; VERDE=""; GIALLO=""; RESET=""; }

problemi=0
QUESTO_SCRIPT=".github/scripts/scan-secrets.sh"

fallisci() {
  problemi=$((problemi + 1))
  printf '%s✗ %s%s\n' "$ROSSO" "$1" "$RESET"
}
ok() { printf '%s✓ %s%s\n' "$VERDE" "$1" "$RESET"; }

# Tutti i file tracciati tranne questo script (che contiene i pattern stessi).
tracciati() { git ls-files | grep -v -F -x "$QUESTO_SCRIPT"; }

# Marcatore di eccezione, per riga.
#
# Alcuni file contengono legittimamente qualcosa che *assomiglia* a una
# credenziale senza esserlo: la regex del detector gemello in
# `.claude/hooks/`, un token finto in un test di regressione, la
# documentazione che descrive i frammenti di riconoscimento. Sono i file che
# parlano di segreti, non quelli che ne contengono.
#
# L'alternativa — escludere quei file per intero — li renderebbe ciechi per
# sempre: un segreto vero aggiunto domani a un test non verrebbe più visto. Il
# marcatore agisce invece sulla **singola riga**, ed è visibile in code review:
# chi lo aggiunge sta dichiarando qualcosa, e chi rilegge il diff lo nota.
MARCATORE='scan-secrets:ok'

# Elenca i file in cui `$1` compare su una riga NON marcata.
cerca() {
  local regex="$1" file
  while IFS= read -r file; do
    if grep -h -E "$regex" "$file" 2>/dev/null | grep -v -F -- "$MARCATORE" | grep -q .; then
      printf '%s\n' "$file"
    fi
  done < <(tracciati)
}

echo "Scansione dei segreti — $(tracciati | wc -l | tr -d ' ') file tracciati"
echo

# -----------------------------------------------------------------------------
# 1. File .env committati
# -----------------------------------------------------------------------------
# `.gitignore` li esclude, ma un `git add -f` o una regola modificata bastano a
# farne entrare uno.
env_committati=$(tracciati | grep -E '(^|/)\.env($|\.)' | grep -v '\.example$' || true)
if [[ -n "$env_committati" ]]; then
  fallisci "File .env tracciati da git:"
  printf '    %s\n' $env_committati
else
  ok "Nessun file .env tracciato (solo .env.example)"
fi

# -----------------------------------------------------------------------------
# 2. Chiave service_role di Supabase
# -----------------------------------------------------------------------------
# Due formati convivono e vanno cercati entrambi.
#
# Legacy: il payload di un JWT Supabase contiene `"role":"service_role"`. In
# base64 la codifica di quella sottostringa dipende dall'offset in cui cade, che
# varia con la lunghezza del `ref` del progetto: i tre frammenti coprono i tre
# allineamenti possibili. Verificato su payload generati casualmente — nessun
# falso positivo sulle chiavi `anon`, che sono pubbliche per progettazione.
#
# Nuovo: `sb_secret_...` è una stringa opaca, non un JWT, quindi i frammenti
# base64 non la intercettano — si riconosce dal prefisso. `sb_publishable_...`
# resta volutamente fuori: è pubblica per progettazione ed è esattamente ciò che
# deve comparire nel frontend.
FRAMMENTI_SERVICE_ROLE='ZXJ2aWNlX3Jv|c2VydmljZV9y|cnZpY2Vfcm9s|sb_secret_[A-Za-z0-9_-]{20,}'
trovati=$(cerca "$FRAMMENTI_SERVICE_ROLE")
if [[ -n "$trovati" ]]; then
  fallisci "Possibile SUPABASE_SERVICE_ROLE_KEY (bypassa il RLS) in:"
  printf '    %s\n' $trovati
  echo "    -> Va revocata e rigenerata dalla dashboard Supabase: una chiave finita"
  echo "       in un commit va considerata compromessa anche se il repo è privato."
else
  ok "Nessuna chiave service_role/sb_secret_ di Supabase nei file tracciati"
fi

# -----------------------------------------------------------------------------
# 3. Token dei provider esterni
# -----------------------------------------------------------------------------
# Pattern con prefisso e lunghezza minima: cercare "token" o "key" produrrebbe
# solo rumore, e un check rumoroso viene disattivato entro una settimana.
declare -A PROVIDER=(
  ["Apify (APIFY_API_TOKEN)"]='apify_api_[A-Za-z0-9]{25,}'
  ["Google/Gemini (chiave AIza)"]='AIza[0-9A-Za-z_-]{35}'
  ["Google/Gemini (chiave AQ.)"]='AQ\.[A-Za-z0-9_-]{30,}'
  ["OpenAI-like (sk-)"]='sk-[A-Za-z0-9]{32,}'
  ["Chiave privata PEM"]='-----BEGIN [A-Z ]*PRIVATE KEY-----'
)
for etichetta in "${!PROVIDER[@]}"; do
  hit=$(cerca "${PROVIDER[$etichetta]}")
  if [[ -n "$hit" ]]; then
    fallisci "Credenziale $etichetta in:"
    printf '    %s\n' $hit
  fi
done
[[ $problemi -eq 0 ]] && ok "Nessun token di provider esterni nei file tracciati"

# -----------------------------------------------------------------------------
# 4. Segreti server-only dentro il codice del frontend
# -----------------------------------------------------------------------------
# Solo file di codice: i `.md` citano legittimamente questi nomi per spiegare
# dove NON devono stare.
file_frontend=$(tracciati | grep -E '^frontend/.*\.(ts|tsx|js|jsx|mjs|cjs|json)$' || true)
if [[ -n "$file_frontend" ]]; then
  SOLO_SERVER='SUPABASE_SERVICE_ROLE_KEY|GEMINI_API_KEY|APIFY_API_TOKEN|CRON_SECRET|SUPABASE_JWT_SECRET'
  hit=$(echo "$file_frontend" | xargs -r grep -l -E "$SOLO_SERVER" 2>/dev/null || true)
  if [[ -n "$hit" ]]; then
    fallisci "Variabili server-only referenziate nel codice del frontend:"
    printf '    %s\n' $hit
    echo "    -> Il frontend viene servito al browser: qualunque valore letto qui"
    echo "       è leggibile da chiunque apra il sito."
  else
    ok "Nessuna variabile server-only nel codice del frontend"
  fi
fi

# -----------------------------------------------------------------------------
# 5. Allowlist delle NEXT_PUBLIC_*
# -----------------------------------------------------------------------------
# Next.js sostituisce ogni `NEXT_PUBLIC_*` a build time con una stringa
# letterale dentro il bundle: l'elenco di quelle ammesse è una decisione di
# sicurezza, non una convenzione.
# NEXT_PUBLIC_TURNSTILE_SITE_KEY: una sitekey Turnstile e' pubblica per
# progetto — sta nell'HTML di chiunque apra la pagina di registrazione, ed e'
# cosi' per progetto di Cloudflare. La meta' che va protetta e' la secret con
# cui si verifica il token, che non vive in questo repo: il signup non passa
# dal backend di Picox, la verifica la fa Supabase Auth.
AMMESSE="NEXT_PUBLIC_SUPABASE_URL NEXT_PUBLIC_SUPABASE_ANON_KEY NEXT_PUBLIC_BACKEND_URL NEXT_PUBLIC_TURNSTILE_SITE_KEY"
usate=$(tracciati | grep -E '^frontend/' | xargs -r grep -oh -E 'NEXT_PUBLIC_[A-Z0-9_]+' 2>/dev/null | sort -u || true)
non_ammesse=""
for nome in $usate; do
  case " $AMMESSE " in
    *" $nome "*) ;;
    *) non_ammesse="$non_ammesse $nome" ;;
  esac
done
if [[ -n "$non_ammesse" ]]; then
  fallisci "Variabili NEXT_PUBLIC_ fuori dall'allowlist:$non_ammesse"
  echo "    -> Se il valore è davvero pubblico, aggiungere il nome all'allowlist"
  echo "       in questo script. Altrimenti va spostato nel backend."
else
  ok "Variabili NEXT_PUBLIC_ conformi all'allowlist ($(echo $usate | wc -w | tr -d ' ') usate)"
fi

# -----------------------------------------------------------------------------
echo
if [[ $problemi -gt 0 ]]; then
  printf '%s%d problema/i rilevato/i.%s\n' "$ROSSO" "$problemi" "$RESET"
  exit 1
fi
printf '%sNessun segreto esposto nei file tracciati.%s\n' "$VERDE" "$RESET"

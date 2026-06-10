#!/bin/bash
# OTel Collector Admin CLI
# Manages secret scopes, init script deployment, IP allowlists, and validation.
#
# Usage:
#   ./manage.sh <command> [options]
#
# Commands:
#   setup           Full setup wizard (scope + secret + deploy + validate)
#   secret-store    Store SP secret in Databricks Secret Scope
#   secret-rotate   Rotate SP secret (store new, warn about old)
#   deploy-script   Upload init script to workspace
#   add-ip          Add current workspace NAT IP to FEVM allowlist
#   bootstrap       Bootstrap Lakebase pool with user token
#   validate        Verify full pipeline health
#   status          Show current configuration state

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${SCRIPT_DIR}/config.env"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
NC='\033[0m'

ok()   { echo -e "  ${GREEN}✓${NC} $1"; }
warn() { echo -e "  ${YELLOW}⚠${NC} $1"; }
err()  { echo -e "  ${RED}✗${NC} $1"; }
info() { echo -e "  ${BLUE}→${NC} $1"; }

# Load config
load_config() {
  if [ ! -f "$CONFIG_FILE" ]; then
    err "Config file not found: $CONFIG_FILE"
    echo "    Copy config.env.example to config.env and fill in values."
    exit 1
  fi
  # shellcheck disable=SC1090
  source "$CONFIG_FILE"
}

# Validate required vars
require_vars() {
  local missing=0
  for var in "$@"; do
    if [ -z "${!var:-}" ]; then
      err "Missing required config: $var"
      missing=1
    fi
  done
  if [ $missing -eq 1 ]; then
    exit 1
  fi
}

# --- Commands ---

cmd_secret_store() {
  load_config
  require_vars CLIENT_WORKSPACE_PROFILES SECRET_SCOPE SECRET_KEY

  echo -e "\n${BLUE}Store SP Secret in Secret Scope${NC}"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

  # Prompt for secret (don't store in config file)
  echo -n "  Enter SP Client Secret: "
  read -rs SP_SECRET
  echo ""

  if [ -z "$SP_SECRET" ]; then
    err "Secret cannot be empty"
    exit 1
  fi

  for PROFILE in $CLIENT_WORKSPACE_PROFILES; do
    info "Workspace profile: $PROFILE"

    # Create scope if not exists
    if databricks secrets list-scopes -p "$PROFILE" 2>/dev/null | grep -q "$SECRET_SCOPE"; then
      ok "Scope '$SECRET_SCOPE' exists"
    else
      info "Creating scope '$SECRET_SCOPE'..."
      databricks secrets create-scope "$SECRET_SCOPE" -p "$PROFILE"
      ok "Scope '$SECRET_SCOPE' created"
    fi

    # Store secret
    echo "$SP_SECRET" | databricks secrets put-secret "$SECRET_SCOPE" "$SECRET_KEY" -p "$PROFILE"
    ok "Secret stored: $SECRET_SCOPE/$SECRET_KEY"
  done

  echo ""
  ok "Secret stored in all workspaces"
}

cmd_secret_rotate() {
  load_config
  require_vars CLIENT_WORKSPACE_PROFILES SECRET_SCOPE SECRET_KEY SP_CLIENT_ID

  echo -e "\n${BLUE}Rotate SP Secret${NC}"
  echo "━━━━━━━━━━━━━━━━━"
  warn "Steps to rotate:"
  echo "    1. Generate new secret in FEVM workspace (Identity > Service Principals > $SP_CLIENT_ID > Secrets)"
  echo "    2. Enter new secret below"
  echo "    3. Test connectivity"
  echo "    4. Delete old secret from FEVM workspace"
  echo ""

  echo -n "  Enter NEW SP Client Secret: "
  read -rs SP_SECRET
  echo ""

  if [ -z "$SP_SECRET" ]; then
    err "Secret cannot be empty"
    exit 1
  fi

  for PROFILE in $CLIENT_WORKSPACE_PROFILES; do
    info "Updating scope in: $PROFILE"
    echo "$SP_SECRET" | databricks secrets put-secret "$SECRET_SCOPE" "$SECRET_KEY" -p "$PROFILE"
    ok "Secret updated: $SECRET_SCOPE/$SECRET_KEY"
  done

  echo ""
  ok "New secret stored. Now validate connectivity:"
  echo "    ./manage.sh validate"
  echo ""
  warn "After validation passes, delete OLD secret from FEVM workspace."
}

cmd_deploy_script() {
  load_config
  require_vars CLIENT_WORKSPACE_PROFILES INIT_SCRIPT_WORKSPACE_PATH

  echo -e "\n${BLUE}Deploy Init Script${NC}"
  echo "━━━━━━━━━━━━━━━━━━━"

  local INIT_SCRIPT="${SCRIPT_DIR}/init_script.sh"
  if [ ! -f "$INIT_SCRIPT" ]; then
    err "Init script not found: $INIT_SCRIPT"
    exit 1
  fi

  for PROFILE in $CLIENT_WORKSPACE_PROFILES; do
    info "Uploading to workspace: $PROFILE"
    info "  Path: $INIT_SCRIPT_WORKSPACE_PATH"

    databricks workspace import "$INIT_SCRIPT_WORKSPACE_PATH" \
      --file "$INIT_SCRIPT" \
      --format AUTO \
      --overwrite \
      -p "$PROFILE"

    ok "Init script deployed to $PROFILE"
  done

  echo ""
  ok "Init script deployed to all workspaces"
  info "Attach to clusters via policy or cluster config:"
  echo "    init_scripts: [{workspace: {destination: $INIT_SCRIPT_WORKSPACE_PATH}}]"
}

cmd_add_ip() {
  load_config
  require_vars FEVM_PROFILE CLIENT_WORKSPACE_PROFILES

  echo -e "\n${BLUE}Add Workspace IP to FEVM Allowlist${NC}"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

  for PROFILE in $CLIENT_WORKSPACE_PROFILES; do
    info "Getting NAT IP for: $PROFILE"

    # Get workspace NAT IP via a simple curl through the workspace
    local WORKSPACE_HOST
    WORKSPACE_HOST=$(databricks auth env -p "$PROFILE" 2>/dev/null | grep DATABRICKS_HOST | cut -d= -f2 | tr -d '"')

    if [ -z "$WORKSPACE_HOST" ]; then
      err "Cannot determine workspace host for profile: $PROFILE"
      continue
    fi

    # The NAT IP is what the workspace uses for outbound connections
    # This requires manual lookup or workspace admin
    warn "Manual step: Add NAT IP for $WORKSPACE_HOST to FEVM IP allowlist"
    echo "    1. Get NAT IP: Workspace Admin > Networking > NAT Gateway IP"
    echo "    2. FEVM Console > Settings > Network > IP Access Lists"
    echo "    3. Add the IP with label: $PROFILE"
  done

  echo ""
  info "After adding IPs, validate with: ./manage.sh validate"
}

cmd_bootstrap() {
  load_config
  require_vars APP_URL FEVM_PROFILE

  echo -e "\n${BLUE}Bootstrap Lakebase Pool${NC}"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━"

  info "Getting user token from FEVM workspace..."
  local TOKEN
  TOKEN=$(databricks auth token -p "$FEVM_PROFILE" 2>/dev/null | tr -d '\n')

  if [ -z "$TOKEN" ]; then
    err "Failed to get token. Run: databricks auth login -p $FEVM_PROFILE"
    exit 1
  fi

  info "Calling bootstrap endpoint..."
  local RESPONSE
  RESPONSE=$(curl -s -w "\n%{http_code}" \
    -X POST "${APP_URL}/api/otel/bootstrap" \
    -H "Authorization: Bearer $TOKEN")

  local HTTP_CODE
  HTTP_CODE=$(echo "$RESPONSE" | tail -1)
  local BODY
  BODY=$(echo "$RESPONSE" | sed '$d')

  if [ "$HTTP_CODE" = "200" ]; then
    ok "Bootstrap successful"
    echo "    $BODY" | python3 -m json.tool 2>/dev/null || echo "    $BODY"
  else
    err "Bootstrap failed (HTTP $HTTP_CODE)"
    echo "    $BODY"
  fi
}

cmd_validate() {
  load_config
  require_vars CLIENT_WORKSPACE_PROFILES SECRET_SCOPE SECRET_KEY SP_CLIENT_ID TOKEN_ENDPOINT APP_URL

  echo -e "\n${BLUE}Validate OTel Pipeline${NC}"
  echo "━━━━━━━━━━━━━━━━━━━━━━━"
  local errors=0

  # 1. Secret scope readable
  for PROFILE in $CLIENT_WORKSPACE_PROFILES; do
    info "Checking scope in: $PROFILE"
    if databricks secrets list-secrets "$SECRET_SCOPE" -p "$PROFILE" 2>/dev/null | grep -q "$SECRET_KEY"; then
      ok "Secret '$SECRET_KEY' exists in scope '$SECRET_SCOPE'"
    else
      err "Secret not found in scope"
      errors=$((errors + 1))
    fi
  done

  # 2. Token generation
  info "Testing SP token generation..."
  local TOKEN
  TOKEN=$(curl -s -X POST "$TOKEN_ENDPOINT" \
    -H "Content-Type: application/x-www-form-urlencoded" \
    -d "grant_type=client_credentials&client_id=${SP_CLIENT_ID}&client_secret=$(databricks secrets get-secret "$SECRET_SCOPE" "$SECRET_KEY" -p "${CLIENT_WORKSPACE_PROFILES%% *}" 2>/dev/null | tr -d '\n')&scope=all-apis" \
    | python3 -c "import sys,json; print(json.load(sys.stdin).get('access_token',''))" 2>/dev/null)

  if [ -n "$TOKEN" ] && [ ${#TOKEN} -gt 100 ]; then
    ok "SP token generated (${#TOKEN} chars)"
  else
    err "Failed to generate SP token"
    errors=$((errors + 1))
  fi

  # 3. Endpoint reachable
  info "Testing app endpoint..."
  local HTTP_CODE
  HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
    -X POST "${APP_URL}/api/otel/v1/metrics" \
    -H "Authorization: Bearer ${TOKEN:-dummy}" \
    -H "Content-Type: application/json" \
    -d '{"resourceMetrics":[]}')

  if [ "$HTTP_CODE" = "200" ] || [ "$HTTP_CODE" = "401" ] || [ "$HTTP_CODE" = "403" ]; then
    ok "App endpoint reachable (HTTP $HTTP_CODE)"
  else
    err "App endpoint unreachable (HTTP $HTTP_CODE) — check IP allowlist"
    errors=$((errors + 1))
  fi

  # 4. Push test metric
  if [ -n "$TOKEN" ] && [ ${#TOKEN} -gt 100 ]; then
    info "Pushing test metric..."
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
      -X POST "${APP_URL}/api/otel/v1/metrics" \
      -H "Authorization: Bearer $TOKEN" \
      -H "Content-Type: application/json" \
      -d '{"resourceMetrics":[{"resource":{"attributes":[{"key":"cluster_id","value":{"stringValue":"validate-test"}},{"key":"instance_id","value":{"stringValue":"validate-node"}},{"key":"is_driver","value":{"stringValue":"true"}},{"key":"node_type","value":{"stringValue":"test"}}]},"scopeMetrics":[{"metrics":[{"name":"system.cpu.load_average.1m","gauge":{"dataPoints":[{"timeUnixNano":"'$(date +%s)000000000'","asDouble":0.01}]}}]}]}]}')

    if [ "$HTTP_CODE" = "200" ]; then
      ok "Test metric accepted (HTTP 200)"
    elif [ "$HTTP_CODE" = "403" ]; then
      err "Test metric rejected — SP not in allowlist (HTTP 403)"
      errors=$((errors + 1))
    else
      err "Test metric failed (HTTP $HTTP_CODE)"
      errors=$((errors + 1))
    fi
  fi

  # Summary
  echo ""
  if [ $errors -eq 0 ]; then
    ok "All checks passed"
  else
    err "$errors check(s) failed"
    exit 1
  fi
}

cmd_status() {
  load_config

  echo -e "\n${BLUE}OTel Configuration Status${NC}"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━"

  echo -e "\n  Config:"
  echo "    FEVM Profile:      ${FEVM_PROFILE:-not set}"
  echo "    Client Profiles:   ${CLIENT_WORKSPACE_PROFILES:-not set}"
  echo "    SP Client ID:      ${SP_CLIENT_ID:-not set}"
  echo "    Token Endpoint:    ${TOKEN_ENDPOINT:-not set}"
  echo "    Secret Scope:      ${SECRET_SCOPE:-not set}"
  echo "    Secret Key:        ${SECRET_KEY:-not set}"
  echo "    App URL:           ${APP_URL:-not set}"
  echo "    Volume Path:       ${VOLUME_PATH:-not set}"
  echo "    Init Script Path:  ${INIT_SCRIPT_WORKSPACE_PATH:-not set}"

  echo -e "\n  Workspace Status:"
  for PROFILE in ${CLIENT_WORKSPACE_PROFILES:-}; do
    echo -n "    $PROFILE: "
    if databricks secrets list-secrets "${SECRET_SCOPE:-otel-collector}" -p "$PROFILE" 2>/dev/null | grep -q "${SECRET_KEY:-sp-client-secret}"; then
      echo -e "${GREEN}scope OK${NC}"
    else
      echo -e "${RED}scope MISSING${NC}"
    fi
  done

  echo -e "\n  App Status:"
  if [ -n "${APP_NAME:-}" ] && [ -n "${FEVM_PROFILE:-}" ]; then
    local STATE
    STATE=$(databricks apps get "$APP_NAME" -p "$FEVM_PROFILE" -o json 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('status',{}).get('state','unknown'))" 2>/dev/null)
    echo "    App State: ${STATE:-unknown}"
  else
    echo "    App State: (config incomplete)"
  fi
}

cmd_setup() {
  echo -e "\n${BLUE}OTel Collector Setup Wizard${NC}"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo ""
  echo "  This will:"
  echo "    1. Create secret scope and store SP secret"
  echo "    2. Deploy init script to workspace"
  echo "    3. Validate the full pipeline"
  echo ""
  echo -n "  Continue? [y/N] "
  read -r CONFIRM
  if [ "$CONFIRM" != "y" ] && [ "$CONFIRM" != "Y" ]; then
    echo "  Aborted."
    exit 0
  fi

  cmd_secret_store
  echo ""
  cmd_deploy_script
  echo ""
  cmd_validate
}

# --- Main ---

usage() {
  echo "Usage: ./manage.sh <command>"
  echo ""
  echo "Commands:"
  echo "  setup           Full setup wizard"
  echo "  secret-store    Store SP secret in Secret Scope"
  echo "  secret-rotate   Rotate SP secret"
  echo "  deploy-script   Upload init script to workspace"
  echo "  add-ip          Show NAT IP instructions for FEVM allowlist"
  echo "  bootstrap       Bootstrap Lakebase pool"
  echo "  validate        Verify full pipeline"
  echo "  status          Show configuration state"
}

case "${1:-}" in
  setup)        cmd_setup ;;
  secret-store) cmd_secret_store ;;
  secret-rotate) cmd_secret_rotate ;;
  deploy-script) cmd_deploy_script ;;
  add-ip)       cmd_add_ip ;;
  bootstrap)    cmd_bootstrap ;;
  validate)     cmd_validate ;;
  status)       cmd_status ;;
  -h|--help)    usage ;;
  *)            usage; exit 1 ;;
esac

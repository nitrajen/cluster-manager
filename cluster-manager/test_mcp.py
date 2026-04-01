"""
Comprehensive MCP server test suite.

Tests each MCP tool by:
1. Calling the underlying REST API endpoint directly
2. Calling the same operation via the MCP JSON-RPC endpoint
3. Comparing the results

Usage:
    python3.11 test_mcp.py
"""

import json
import sys
import traceback
from pathlib import Path

# Add project root to path so imports work
sys.path.insert(0, str(Path(__file__).parent))

from fastapi.testclient import TestClient

from cluster_manager.backend.app import app

# NOTE: client is set in main() after context manager setup
client: TestClient = None  # type: ignore

# ── helpers ───────────────────────────────────────────────────────────────────

GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
RESET  = "\033[0m"

def ok(msg):   print(f"  {GREEN}✓{RESET} {msg}")
def fail(msg): print(f"  {RED}✗{RESET} {msg}")
def warn(msg): print(f"  {YELLOW}~{RESET} {msg}")

def mcp_call(method: str, params: dict | None = None, req_id: int = 1) -> dict:
    """Make an MCP JSON-RPC 2.0 call."""
    payload = {"jsonrpc": "2.0", "method": method, "id": req_id}
    if params is not None:
        payload["params"] = params
    resp = client.post("/api/mcp", json=payload)
    return resp.json()

def extract_mcp_result(mcp_resp: dict):
    """Extract the tool result from an MCP tools/call response."""
    if "error" in mcp_resp and mcp_resp["error"]:
        return None, mcp_resp["error"]
    content = mcp_resp.get("result", {}).get("content", [])
    if content:
        return json.loads(content[0]["text"]), None
    return mcp_resp.get("result"), None

def compare_keys(a: dict, b: dict, context: str = "") -> list[str]:
    """Return list of top-level key mismatches between two dicts."""
    mismatches = []
    a_keys = set(a.keys()) if isinstance(a, dict) else set()
    b_keys = set(b.keys()) if isinstance(b, dict) else set()
    if a_keys != b_keys:
        only_a = a_keys - b_keys
        only_b = b_keys - a_keys
        if only_a:
            mismatches.append(f"{context} keys only in REST: {only_a}")
        if only_b:
            mismatches.append(f"{context} keys only in MCP: {only_b}")
    return mismatches


# ── results tracker ───────────────────────────────────────────────────────────

results = []  # list of (tool_name, api_status, mcp_status, match, notes)

def record(tool, api_ok, mcp_ok, match, notes=""):
    results.append((tool, api_ok, mcp_ok, match, notes))


# ── test functions ─────────────────────────────────────────────────────────────

def test_mcp_health():
    print("\n=== MCP Health Check ===")
    resp = client.get("/api/mcp/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "healthy"
    ok(f"Health: {data}")


def test_mcp_initialize():
    print("\n=== MCP initialize ===")
    resp = mcp_call("initialize")
    assert "error" not in resp or not resp["error"], f"Error: {resp.get('error')}"
    result = resp["result"]
    assert result["protocolVersion"] == "2024-11-05"
    assert "tools" in result["capabilities"]
    ok(f"Protocol: {result['protocolVersion']}, Server: {result['serverInfo']['name']}")


def test_tools_list():
    print("\n=== MCP tools/list ===")
    resp = mcp_call("tools/list")
    assert "error" not in resp or not resp["error"]
    tools = resp["result"]["tools"]
    names = [t["name"] for t in tools]
    expected = ["list_clusters", "get_cluster", "start_cluster", "stop_cluster",
                "get_cluster_events", "list_policies", "get_policy"]
    missing = set(expected) - set(names)
    extra = set(names) - set(expected)
    if missing:
        fail(f"Missing tools: {missing}")
    else:
        ok(f"All 7 tools present: {names}")
    if extra:
        warn(f"Extra tools (unexpected): {extra}")
    return names


def test_list_clusters(cluster_ids: list) -> str | None:
    """Test list_clusters - returns first cluster_id for downstream tests."""
    print("\n=== Tool: list_clusters ===")

    # REST API
    api_resp = client.get("/api/clusters?limit=10")
    api_ok = api_resp.status_code == 200
    api_data = api_resp.json() if api_ok else None

    # MCP
    mcp_resp = mcp_call("tools/call", {"name": "list_clusters", "arguments": {"limit": 10}})
    mcp_data, mcp_err = extract_mcp_result(mcp_resp)
    mcp_ok = mcp_err is None and mcp_data is not None

    notes = []

    if not api_ok:
        fail(f"REST API failed: {api_resp.status_code} {api_resp.text[:100]}")
        notes.append(f"REST HTTP {api_resp.status_code}")
    else:
        ok(f"REST: {len(api_data)} clusters returned")

    if not mcp_ok:
        fail(f"MCP failed: {mcp_err}")
        notes.append(f"MCP error: {mcp_err}")
    else:
        ok(f"MCP: {mcp_data['count']} clusters returned")
        # Collect cluster IDs for downstream tests
        for c in mcp_data.get("clusters", []):
            if c.get("cluster_id"):
                cluster_ids.append(c["cluster_id"])

    match = False
    if api_ok and mcp_ok:
        api_count = len(api_data)
        mcp_count = mcp_data["count"]
        if api_count == mcp_count:
            match = True
            ok(f"COUNT MATCH: both return {api_count} clusters")
        else:
            fail(f"COUNT MISMATCH: REST={api_count}, MCP={mcp_count}")
            notes.append(f"count mismatch: REST={api_count} MCP={mcp_count}")

        # Compare cluster IDs in both results
        api_ids = {c["cluster_id"] for c in api_data if c.get("cluster_id")}
        mcp_ids = {c["cluster_id"] for c in mcp_data.get("clusters", []) if c.get("cluster_id")}
        if api_ids == mcp_ids:
            ok("Cluster ID sets match")
        else:
            fail(f"ID mismatch: only_rest={api_ids - mcp_ids}, only_mcp={mcp_ids - api_ids}")
            match = False

        # Check first cluster has same schema
        if api_data and mcp_data.get("clusters"):
            mismatches = compare_keys(api_data[0], mcp_data["clusters"][0], "cluster")
            if mismatches:
                warn(f"Schema diffs: {mismatches}")
            else:
                ok("Cluster schema matches between REST and MCP")

    record("list_clusters", api_ok, mcp_ok, match, ", ".join(notes))
    return cluster_ids[0] if cluster_ids else None


def test_get_cluster(cluster_id: str | None):
    print(f"\n=== Tool: get_cluster (id={cluster_id}) ===")

    if not cluster_id:
        warn("Skipping - no cluster_id available")
        record("get_cluster", None, None, None, "skipped - no cluster_id")
        return

    # REST API
    api_resp = client.get(f"/api/clusters/{cluster_id}")
    api_ok = api_resp.status_code == 200
    api_data = api_resp.json() if api_ok else None

    # MCP
    mcp_resp = mcp_call("tools/call", {"name": "get_cluster", "arguments": {"cluster_id": cluster_id}})
    mcp_data, mcp_err = extract_mcp_result(mcp_resp)
    mcp_ok = mcp_err is None and mcp_data is not None

    notes = []

    if not api_ok:
        fail(f"REST failed: {api_resp.status_code}")
        notes.append(f"REST HTTP {api_resp.status_code}")
    else:
        ok(f"REST: cluster '{api_data.get('cluster_name')}' state={api_data.get('state')}")

    if not mcp_ok:
        fail(f"MCP failed: {mcp_err}")
        notes.append(f"MCP error: {mcp_err}")
    else:
        ok(f"MCP: cluster '{mcp_data.get('cluster_name')}' state={mcp_data.get('state')}")

    match = False
    if api_ok and mcp_ok:
        # Compare key fields
        fields = ["cluster_id", "cluster_name", "state", "node_type_id", "spark_version"]
        mismatches = []
        for f in fields:
            if api_data.get(f) != mcp_data.get(f):
                mismatches.append(f"{f}: REST={api_data.get(f)!r} MCP={mcp_data.get(f)!r}")
        if mismatches:
            fail(f"Field mismatches: {mismatches}")
            notes.extend(mismatches)
        else:
            match = True
            ok(f"Key fields match: {fields}")

    record("get_cluster", api_ok, mcp_ok, match, ", ".join(notes))


def test_get_cluster_events(cluster_id: str | None):
    print(f"\n=== Tool: get_cluster_events (id={cluster_id}) ===")

    if not cluster_id:
        warn("Skipping - no cluster_id available")
        record("get_cluster_events", None, None, None, "skipped - no cluster_id")
        return

    # REST API
    api_resp = client.get(f"/api/clusters/{cluster_id}/events?limit=10")
    api_ok = api_resp.status_code == 200
    api_data = api_resp.json() if api_ok else None

    # MCP
    mcp_resp = mcp_call("tools/call", {
        "name": "get_cluster_events",
        "arguments": {"cluster_id": cluster_id, "limit": 10}
    })
    mcp_data, mcp_err = extract_mcp_result(mcp_resp)
    mcp_ok = mcp_err is None and mcp_data is not None

    notes = []

    if not api_ok:
        fail(f"REST failed: {api_resp.status_code}")
        notes.append(f"REST HTTP {api_resp.status_code}")
    else:
        ok(f"REST: {api_data.get('total_count')} events returned")

    if not mcp_ok:
        fail(f"MCP failed: {mcp_err}")
        notes.append(f"MCP error: {mcp_err}")
    else:
        ok(f"MCP: {mcp_data.get('total_count')} events returned")

    match = False
    if api_ok and mcp_ok:
        if api_data.get("total_count") == mcp_data.get("total_count"):
            match = True
            ok(f"Event count matches: {api_data['total_count']}")
        else:
            fail(f"Count mismatch: REST={api_data.get('total_count')} MCP={mcp_data.get('total_count')}")
            notes.append(f"count mismatch")

        # Compare first event structure if any
        api_events = api_data.get("events", [])
        mcp_events = mcp_data.get("events", [])
        if api_events and mcp_events:
            mismatches = compare_keys(api_events[0], mcp_events[0], "event")
            if mismatches:
                warn(f"Event schema diffs: {mismatches}")
            else:
                ok("Event schema matches")

    record("get_cluster_events", api_ok, mcp_ok, match, ", ".join(notes))


def test_list_policies(policy_ids: list) -> str | None:
    print("\n=== Tool: list_policies ===")

    # REST API
    api_resp = client.get("/api/policies")
    api_ok = api_resp.status_code == 200
    api_data = api_resp.json() if api_ok else None

    # MCP
    mcp_resp = mcp_call("tools/call", {"name": "list_policies", "arguments": {}})
    mcp_data, mcp_err = extract_mcp_result(mcp_resp)
    mcp_ok = mcp_err is None and mcp_data is not None

    notes = []

    if not api_ok:
        fail(f"REST failed: {api_resp.status_code}")
        notes.append(f"REST HTTP {api_resp.status_code}")
    else:
        ok(f"REST: {len(api_data)} policies returned")

    if not mcp_ok:
        fail(f"MCP failed: {mcp_err}")
        notes.append(f"MCP error: {mcp_err}")
    else:
        ok(f"MCP: {mcp_data['count']} policies returned")
        for p in mcp_data.get("policies", []):
            if p.get("policy_id"):
                policy_ids.append(p["policy_id"])

    match = False
    if api_ok and mcp_ok:
        api_count = len(api_data)
        mcp_count = mcp_data["count"]
        if api_count == mcp_count:
            match = True
            ok(f"COUNT MATCH: both return {api_count} policies")
        else:
            fail(f"COUNT MISMATCH: REST={api_count}, MCP={mcp_count}")
            notes.append(f"count mismatch: REST={api_count} MCP={mcp_count}")

        # Compare policy IDs
        api_ids = {p["policy_id"] for p in api_data if p.get("policy_id")}
        mcp_ids = {p["policy_id"] for p in mcp_data.get("policies", []) if p.get("policy_id")}
        if api_ids == mcp_ids:
            ok("Policy ID sets match")
        else:
            fail(f"ID mismatch: only_rest={api_ids - mcp_ids}, only_mcp={mcp_ids - api_ids}")
            match = False

    record("list_policies", api_ok, mcp_ok, match, ", ".join(notes))
    return policy_ids[0] if policy_ids else None


def test_get_policy(policy_id: str | None):
    print(f"\n=== Tool: get_policy (id={policy_id}) ===")

    if not policy_id:
        warn("Skipping - no policy_id available")
        record("get_policy", None, None, None, "skipped - no policy_id")
        return

    # REST API
    api_resp = client.get(f"/api/policies/{policy_id}")
    api_ok = api_resp.status_code == 200
    api_data = api_resp.json() if api_ok else None

    # MCP
    mcp_resp = mcp_call("tools/call", {"name": "get_policy", "arguments": {"policy_id": policy_id}})
    mcp_data, mcp_err = extract_mcp_result(mcp_resp)
    mcp_ok = mcp_err is None and mcp_data is not None

    notes = []

    if not api_ok:
        fail(f"REST failed: {api_resp.status_code}")
        notes.append(f"REST HTTP {api_resp.status_code}")
    else:
        ok(f"REST: policy '{api_data.get('name')}'")

    if not mcp_ok:
        fail(f"MCP failed: {mcp_err}")
        notes.append(f"MCP error: {mcp_err}")
    else:
        ok(f"MCP: policy '{mcp_data.get('name')}'")

    match = False
    if api_ok and mcp_ok:
        fields = ["policy_id", "name", "is_default", "creator_user_name"]
        mismatches = []
        for f in fields:
            if api_data.get(f) != mcp_data.get(f):
                mismatches.append(f"{f}: REST={api_data.get(f)!r} MCP={mcp_data.get(f)!r}")
        if mismatches:
            fail(f"Field mismatches: {mismatches}")
            notes.extend(mismatches)
        else:
            match = True
            ok(f"Key fields match: {fields}")

    record("get_policy", api_ok, mcp_ok, match, ", ".join(notes))


def test_start_cluster_skipped():
    """start_cluster - skip (would incur costs and interrupt cluster state)."""
    print("\n=== Tool: start_cluster ===")
    warn("SKIPPED - would incur compute costs. Manual verification required.")
    warn("To test: call POST /api/clusters/{id}/start and compare with")
    warn("  MCP tools/call {name: start_cluster, arguments: {cluster_id: id}}")
    record("start_cluster", None, None, None, "skipped - would incur compute costs")


def test_stop_cluster_skipped():
    """stop_cluster - skip (would interrupt running clusters)."""
    print("\n=== Tool: stop_cluster ===")
    warn("SKIPPED - would interrupt running clusters. Manual verification required.")
    warn("To test: call POST /api/clusters/{id}/stop and compare with")
    warn("  MCP tools/call {name: stop_cluster, arguments: {cluster_id: id}}")
    record("stop_cluster", None, None, None, "skipped - would interrupt running clusters")


def test_mcp_error_handling():
    """Test MCP error handling for invalid inputs."""
    print("\n=== MCP Error Handling ===")

    # Invalid JSON-RPC version
    resp = client.post("/api/mcp", json={"jsonrpc": "1.0", "method": "tools/list", "id": 1})
    data = resp.json()
    assert data.get("error", {}).get("code") == -32600, f"Expected -32600, got {data}"
    ok("Invalid JSON-RPC version → error -32600")

    # Unknown method
    resp = mcp_call("unknown/method")
    data = resp
    assert data.get("error", {}).get("code") == -32601, f"Expected -32601, got {data}"
    ok("Unknown method → error -32601")

    # Unknown tool
    resp = mcp_call("tools/call", {"name": "nonexistent_tool", "arguments": {}})
    data = resp
    assert data.get("error", {}).get("code") == -32602, f"Expected -32602, got {data}"
    ok("Unknown tool → error -32602")

    # Missing required parameter
    resp = mcp_call("tools/call", {"name": "get_cluster", "arguments": {}})
    data = resp
    assert data.get("error") is not None, "Expected error for missing cluster_id"
    ok("Missing required cluster_id → error returned")

    # Invalid cluster_id
    resp = mcp_call("tools/call", {"name": "get_cluster", "arguments": {"cluster_id": "invalid-xxx"}})
    data = resp
    mcp_data, mcp_err = extract_mcp_result(data)
    assert mcp_err is not None, "Expected error for invalid cluster_id"
    ok(f"Invalid cluster_id → error: {mcp_err['message'][:60]}...")


def test_list_clusters_with_filter():
    """Test list_clusters with state filter."""
    print("\n=== list_clusters with state filter ===")

    # REST
    api_resp = client.get("/api/clusters?state=RUNNING&limit=10")
    api_ok = api_resp.status_code == 200

    # MCP
    mcp_resp = mcp_call("tools/call", {"name": "list_clusters", "arguments": {"state": "RUNNING", "limit": 10}})
    mcp_data, mcp_err = extract_mcp_result(mcp_resp)
    mcp_ok = mcp_err is None

    notes = []
    if api_ok and mcp_ok:
        api_running = api_resp.json()
        mcp_running = mcp_data.get("clusters", [])
        # All returned clusters should be RUNNING
        api_states = {c.get("state") for c in api_running}
        mcp_states = {c.get("state") for c in mcp_running}

        if api_states and api_states != {"RUNNING"}:
            fail(f"REST returned non-RUNNING states: {api_states}")
            notes.append("REST filter bug")
        else:
            ok(f"REST state filter works: {len(api_running)} RUNNING clusters")

        if mcp_states and mcp_states != {"RUNNING"}:
            fail(f"MCP returned non-RUNNING states: {mcp_states}")
            notes.append("MCP filter bug")
        else:
            ok(f"MCP state filter works: {len(mcp_running)} RUNNING clusters")

        if len(api_running) == mcp_data.get("count"):
            ok("Filtered counts match")
        else:
            fail(f"Count mismatch: REST={len(api_running)}, MCP={mcp_data.get('count')}")
            notes.append("filter count mismatch")

    record("list_clusters[RUNNING]", api_ok, mcp_ok, api_ok and mcp_ok and not notes, ", ".join(notes))


# ── summary ────────────────────────────────────────────────────────────────────

def print_summary():
    print("\n" + "="*80)
    print("SUMMARY TABLE")
    print("="*80)
    header = f"{'Tool':<25} {'REST API':<12} {'MCP':<12} {'Match':<8} {'Notes'}"
    print(header)
    print("-"*80)

    all_pass = True
    for tool, api_ok, mcp_ok, match, notes in results:
        def fmt(v):
            if v is True:  return f"{GREEN}PASS{RESET}"
            if v is False: return f"{RED}FAIL{RESET}"
            return f"{YELLOW}SKIP{RESET}"

        match_str = fmt(match)
        row = f"{tool:<25} {fmt(api_ok):<20} {fmt(mcp_ok):<20} {match_str:<16} {notes[:40]}"
        print(row)
        if match is False:
            all_pass = False

    print("="*80)
    if all_pass:
        print(f"{GREEN}All tests passed (or skipped){RESET}")
    else:
        print(f"{RED}Some tests FAILED - see details above{RESET}")
    return all_pass


# ── main ───────────────────────────────────────────────────────────────────────

def main():
    global client
    print("Databricks Cluster Manager - MCP Server Test Suite")
    print("="*60)

    cluster_ids = []
    policy_ids = []

    # Use context manager to ensure lifespan runs (workspace client init)
    with TestClient(app, raise_server_exceptions=False) as tc:
        client = tc
        return _run_tests(cluster_ids, policy_ids)


def _run_tests(cluster_ids, policy_ids):
    try:
        test_mcp_health()
        test_mcp_initialize()
        test_tools_list()

        # Read tests
        first_cluster_id = test_list_clusters(cluster_ids)
        test_get_cluster(first_cluster_id)
        test_get_cluster_events(first_cluster_id)
        test_list_clusters_with_filter()

        first_policy_id = test_list_policies(policy_ids)
        test_get_policy(first_policy_id)

        # Skip destructive tests
        test_start_cluster_skipped()
        test_stop_cluster_skipped()

        # Error handling
        test_mcp_error_handling()

    except Exception as e:
        print(f"\n{RED}FATAL ERROR:{RESET} {e}")
        traceback.print_exc()
        sys.exit(1)

    success = print_summary()
    sys.exit(0 if success else 1)
    return success


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Mock OTel Producer — simulates cluster nodes sending metrics to the collector.

Generates realistic CPU/memory/disk/network metrics and POSTs them as
OTLP/HTTP JSON to the cluster-manager app's receiver endpoint.

Usage:
    # Against local dev server
    python mock_producer.py --endpoint http://localhost:8000

    # Against deployed app
    python mock_producer.py --endpoint https://cluster-manager-xxx.databricksapps.com

    # Custom cluster simulation
    python mock_producer.py --endpoint http://localhost:8000 --clusters 3 --workers 4 --interval 15
"""

import argparse
import json
import math
import random
import time
from datetime import datetime, timezone

import requests


def generate_node_id(cluster_idx: int, node_idx: int, is_driver: bool) -> str:
    """Generate a realistic-looking instance ID."""
    suffix = f"{cluster_idx:02d}{node_idx:02d}"
    return f"i-0mock{'d' if is_driver else 'w'}{suffix}abc{random.randint(100, 999)}"


def generate_cluster_id(idx: int) -> str:
    """Generate a realistic cluster ID."""
    return f"0610-mock-cluster-{idx:04d}"


NODE_TYPES = ["m5.xlarge", "m5.2xlarge", "r5.xlarge", "c5.2xlarge", "i3.xlarge"]


class NodeSimulator:
    """Simulates a single node with trending metrics."""

    def __init__(self, cluster_id: str, instance_id: str, is_driver: bool, node_type: str):
        self.cluster_id = cluster_id
        self.instance_id = instance_id
        self.is_driver = is_driver
        self.node_type = node_type

        # Base levels (vary per node)
        self.base_cpu_user = random.uniform(10, 50)
        self.base_cpu_system = random.uniform(3, 15)
        self.base_mem = random.uniform(40, 75)
        self.base_disk = random.uniform(20, 60)

        # Trends
        self._tick = 0
        self._spike_until = 0

    def generate_metrics(self) -> dict:
        """Generate one data point with realistic variation."""
        self._tick += 1

        # Occasional CPU spike (10% chance, lasts 3-5 ticks)
        if random.random() < 0.03 and self._tick > self._spike_until:
            self._spike_until = self._tick + random.randint(3, 8)

        in_spike = self._tick < self._spike_until

        # CPU with sinusoidal variation + noise
        wave = math.sin(self._tick * 0.1) * 5
        noise = random.gauss(0, 3)
        spike_add = random.uniform(20, 40) if in_spike else 0

        cpu_user = max(0, min(100, self.base_cpu_user + wave + noise + spike_add))
        cpu_system = max(0, min(100 - cpu_user, self.base_cpu_system + random.gauss(0, 2)))
        cpu_wait = max(0, random.gauss(1.5, 1.0))

        # Memory — slowly drifts
        mem_drift = math.sin(self._tick * 0.02) * 8
        mem = max(0, min(99, self.base_mem + mem_drift + random.gauss(0, 2)))
        mem_swap = max(0, random.gauss(0.5, 0.5)) if mem > 85 else 0

        # Network — bursty
        net_sent = int(random.expovariate(1 / 500_000))  # avg 500KB
        net_recv = int(random.expovariate(1 / 800_000))  # avg 800KB

        # Disk — slow growth
        disk = max(0, min(99, self.base_disk + self._tick * 0.01 + random.gauss(0, 1)))

        # Load average
        total_cpu = (cpu_user + cpu_system) / 100
        load_1m = max(0, total_cpu * 4 + random.gauss(0, 0.3))
        load_5m = max(0, total_cpu * 4 + random.gauss(0, 0.1))
        load_15m = max(0, total_cpu * 4 + random.gauss(0, 0.05))

        return {
            "cpu_user": cpu_user / 100,  # OTel uses 0-1
            "cpu_system": cpu_system / 100,
            "cpu_wait": cpu_wait / 100,
            "mem": mem / 100,
            "mem_swap": mem_swap / 100,
            "net_sent": net_sent,
            "net_recv": net_recv,
            "disk": disk / 100,
            "load_1m": load_1m,
            "load_5m": load_5m,
            "load_15m": load_15m,
        }


def build_otlp_payload(nodes: list[NodeSimulator]) -> dict:
    """Build OTLP/HTTP JSON ExportMetricsServiceRequest."""
    now_ns = str(int(time.time() * 1e9))

    resource_metrics = []
    for node in nodes:
        metrics = node.generate_metrics()

        resource = {
            "attributes": [
                {"key": "cluster_id", "value": {"stringValue": node.cluster_id}},
                {"key": "instance_id", "value": {"stringValue": node.instance_id}},
                {"key": "is_driver", "value": {"stringValue": str(node.is_driver).lower()}},
                {"key": "node_type", "value": {"stringValue": node.node_type}},
            ]
        }

        scope_metrics = {
            "scope": {"name": "otel-mock-producer", "version": "1.0.0"},
            "metrics": [
                {
                    "name": "system.cpu.utilization",
                    "gauge": {"dataPoints": [
                        {
                            "timeUnixNano": now_ns,
                            "asDouble": metrics["cpu_user"],
                            "attributes": [{"key": "state", "value": {"stringValue": "user"}}],
                        },
                        {
                            "timeUnixNano": now_ns,
                            "asDouble": metrics["cpu_system"],
                            "attributes": [{"key": "state", "value": {"stringValue": "system"}}],
                        },
                        {
                            "timeUnixNano": now_ns,
                            "asDouble": metrics["cpu_wait"],
                            "attributes": [{"key": "state", "value": {"stringValue": "wait"}}],
                        },
                    ]},
                },
                {
                    "name": "system.memory.utilization",
                    "gauge": {"dataPoints": [{
                        "timeUnixNano": now_ns,
                        "asDouble": metrics["mem"],
                    }]},
                },
                {
                    "name": "system.paging.utilization",
                    "gauge": {"dataPoints": [{
                        "timeUnixNano": now_ns,
                        "asDouble": metrics["mem_swap"],
                    }]},
                },
                {
                    "name": "system.network.io",
                    "sum": {"dataPoints": [
                        {
                            "timeUnixNano": now_ns,
                            "asInt": str(metrics["net_sent"]),
                            "attributes": [{"key": "direction", "value": {"stringValue": "transmit"}}],
                        },
                        {
                            "timeUnixNano": now_ns,
                            "asInt": str(metrics["net_recv"]),
                            "attributes": [{"key": "direction", "value": {"stringValue": "receive"}}],
                        },
                    ]},
                },
                {
                    "name": "system.disk.utilization",
                    "gauge": {"dataPoints": [{
                        "timeUnixNano": now_ns,
                        "asDouble": metrics["disk"],
                    }]},
                },
                {
                    "name": "system.cpu.load_average.1m",
                    "gauge": {"dataPoints": [{
                        "timeUnixNano": now_ns,
                        "asDouble": metrics["load_1m"],
                    }]},
                },
                {
                    "name": "system.cpu.load_average.5m",
                    "gauge": {"dataPoints": [{
                        "timeUnixNano": now_ns,
                        "asDouble": metrics["load_5m"],
                    }]},
                },
                {
                    "name": "system.cpu.load_average.15m",
                    "gauge": {"dataPoints": [{
                        "timeUnixNano": now_ns,
                        "asDouble": metrics["load_15m"],
                    }]},
                },
            ],
        }

        resource_metrics.append({
            "resource": resource,
            "scopeMetrics": [scope_metrics],
        })

    return {"resourceMetrics": resource_metrics}


def main():
    parser = argparse.ArgumentParser(description="Mock OTel metrics producer")
    parser.add_argument("--endpoint", default="http://localhost:8000",
                        help="Collector app base URL")
    parser.add_argument("--clusters", type=int, default=2,
                        help="Number of simulated clusters")
    parser.add_argument("--workers", type=int, default=3,
                        help="Number of worker nodes per cluster")
    parser.add_argument("--interval", type=int, default=15,
                        help="Seconds between metric pushes")
    parser.add_argument("--token", default="mock-dev-token",
                        help="Bearer token for auth")
    parser.add_argument("--bursts", type=int, default=0,
                        help="If >0, send N bursts then exit (for testing)")
    args = parser.parse_args()

    url = f"{args.endpoint.rstrip('/')}/api/otel/v1/metrics"

    # Create simulated nodes
    nodes: list[NodeSimulator] = []
    for c in range(args.clusters):
        cluster_id = generate_cluster_id(c)
        # Driver
        nodes.append(NodeSimulator(
            cluster_id=cluster_id,
            instance_id=generate_node_id(c, 0, True),
            is_driver=True,
            node_type=random.choice(NODE_TYPES),
        ))
        # Workers
        for w in range(args.workers):
            nodes.append(NodeSimulator(
                cluster_id=cluster_id,
                instance_id=generate_node_id(c, w + 1, False),
                is_driver=False,
                node_type=random.choice(NODE_TYPES),
            ))

    total_nodes = len(nodes)
    print(f"🚀 Mock OTel Producer")
    print(f"   Endpoint: {url}")
    print(f"   Clusters: {args.clusters}")
    print(f"   Nodes:    {total_nodes} ({args.clusters} drivers + {args.clusters * args.workers} workers)")
    print(f"   Interval: {args.interval}s")
    print(f"   Token:    {args.token[:20]}...")
    print()

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {args.token}",
    }

    tick = 0
    max_ticks = args.bursts if args.bursts > 0 else float("inf")

    try:
        while tick < max_ticks:
            tick += 1
            payload = build_otlp_payload(nodes)
            payload_size = len(json.dumps(payload))

            try:
                resp = requests.post(url, json=payload, headers=headers, timeout=10)
                ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                if resp.status_code == 200:
                    print(f"[{ts}] ✓ Sent {total_nodes} nodes ({payload_size:,} bytes) → 200 OK")
                else:
                    print(f"[{ts}] ✗ HTTP {resp.status_code}: {resp.text[:100]}")
            except requests.exceptions.ConnectionError:
                ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                print(f"[{ts}] ✗ Connection refused — is the collector running at {args.endpoint}?")
            except Exception as e:
                ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                print(f"[{ts}] ✗ Error: {e}")

            if tick < max_ticks:
                time.sleep(args.interval)

    except KeyboardInterrupt:
        print(f"\n⏹  Stopped after {tick} sends")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
import subprocess
import sys
import time
import json
import random
import ast
from collections import defaultdict

try:
    import yaml  # pip install pyyaml
except ImportError:
    print("[ERROR] Missing dependency: PyYAML. Install with: pip install pyyaml")
    sys.exit(1)

# ----------------------------
# CONFIGURATION
# ----------------------------
NETWORKS = {
    "mgmt": "172.21.76.0/24:kind",
    "core": "172.21.76.0/24:kind",
    "regional": "172.21.76.0/24:kind",
    "edge": "172.21.76.0/24:kind",
}

CLUSTERS_YAML = {
    "mgmt": "infra-kind/mgmt-cluster.yaml",
    "core": "infra-kind/core-cluster.yaml",
    "regional": "infra-kind/regional-cluster.yaml",
    "edge": "infra-kind/edge-cluster.yaml",
}

# Network CR generation settings
TOPOLOGY_NAME = "5g"
DEFAULT_NODE = "leaf"
DEFAULT_REGION = "region1"
DEFAULT_SITE = "site1"
ENDPOINT_BASE={"core":"e1-1","regional":"e1-2","edge":"e1-3"}
OUTPUT_CRS_FILE = "network-crs.yaml"

# ----------------------------
# HELPERS
# ----------------------------
def run(cmd, check=True, capture=False):
    if capture:
        return subprocess.check_output(cmd, shell=True, text=True).strip()
    return subprocess.run(cmd, shell=True, check=check)

def log(level, msg):
    colors = {
        "INFO": "\033[1;34m",
        "WARN": "\033[1;33m",
        "ERROR": "\033[1;31m",
        "OK": "\033[1;32m",
    }
    color = colors.get(level, "")
    reset = "\033[0m"
    print(f"{color}[{level}]{reset} {msg}")

# ----------------------------
# NETWORKS
# ----------------------------
def create_networks():
    log("INFO", "Creating networks...")
    for net, config in NETWORKS.items():
        subnet, bridge = config.split(":")
        try:
            run(f"docker network inspect {bridge}", check=True)
            log("INFO", f"Network {bridge} already exists, skipping.")
        except subprocess.CalledProcessError:
            log("INFO", f"Creating network {bridge} ({subnet})")
            run(
                f"docker network create --driver=bridge --subnet={subnet} "
                f"--opt com.docker.network.bridge.name={bridge} {bridge}",
                check=True
            )

def delete_networks():
    log("INFO", "Deleting networks...")
    for net, config in NETWORKS.items():
        bridge = config.split(":")[1]
        try:
            run(f"docker network inspect {bridge}", check=True)
            log("INFO", f"Removing network {bridge}")
            run(f"docker network rm {bridge}", check=False)
        except subprocess.CalledProcessError:
            log("INFO", f"Network {bridge} does not exist, skipping.")

# ----------------------------
# CLUSTERS
# ----------------------------
def create_kind_cluster(cluster):
    bridge = NETWORKS[cluster].split(":")[1]
    clusters = run("kind get clusters", capture=True)
    if cluster in clusters.split("\n"):
        log("INFO", f"Cluster {cluster} already exists, skipping.")
        return

    log("INFO", f"Creating kind cluster {cluster} on network {bridge}")
    run(
        f"KIND_EXPERIMENTAL_DOCKER_NETWORK={bridge} "
        f"kind create cluster --config={CLUSTERS_YAML[cluster]} --name {cluster} --wait 5m"
    )
    run(f"kubectl get nodes --context kind-{cluster}")
    run("kubectl create -f infra-kind/multus-daemonset-thick.yml")
    log("INFO", f"Installing multus on {cluster}")

    # Label worker nodes
    try:
        worker_nodes = run(
            f"kubectl get nodes --context kind-{cluster} "
            f"-l '!node-role.kubernetes.io/control-plane' "
            f"-o name", capture=True
        ).splitlines()

        for node in worker_nodes:
            run(f"kubectl label node --overwrite {node} node-role.kubernetes.io/worker= --context kind-{cluster}")
            log("OK", f"Labeled {node} as worker")
    except subprocess.CalledProcessError:
        log("WARN", f"Failed to label worker nodes in {cluster}")

    # Download CNI plugins and copy into all nodes
    CNI_VERSION = "v1.3.0"
    ARCH = "amd64"
    log("INFO", f"Downloading CNI plugins {CNI_VERSION}")
    run(f"curl -L -o /tmp/cni-plugins.tgz https://github.com/containernetworking/plugins/releases/download/{CNI_VERSION}/cni-plugins-linux-{ARCH}-{CNI_VERSION}.tgz")
    run("mkdir -p /tmp/cni-plugins")
    run("tar -xzf /tmp/cni-plugins.tgz -C /tmp/cni-plugins")

    nodes = run(f"kind get nodes --name {cluster}", capture=True).splitlines()
    for node in nodes:
        log("INFO", f"Copying CNI plugins to {node}:/opt/cni/bin")
        run(f"docker cp /tmp/cni-plugins/. {node}:/opt/cni/bin/")

    log("OK", f"Kind cluster {cluster} created with Multus and all CNI plugins installed ✅")

def delete_kind_clusters():
    log("INFO", "Deleting kind clusters...")
    clusters = run("kind get clusters", capture=True).split("\n")
    for cluster in CLUSTERS_YAML.keys():
        if cluster in clusters:
            log("INFO", f"Deleting cluster {cluster}")
            run(f"kind delete cluster --name {cluster}")
        else:
            log("INFO", f"Cluster {cluster} does not exist, skipping.")

def wait_for_clusters(timeout=300, poll_interval=5):
    log("INFO", f"Waiting for clusters to be ready (timeout {timeout}s)...")
    clusters = run("kind get clusters", capture=True).split("\n")

    for cluster in CLUSTERS_YAML.keys():
        if cluster not in clusters:
            log("WARN", f"Cluster {cluster} does not exist, skipping.")
            continue

        log("INFO", f"Checking cluster {cluster}...")
        start = time.time()

        while True:
            try:
                nodes_json = json.loads(
                    run(f"kubectl get nodes -o json --context kind-{cluster}", capture=True)
                )
                not_ready_nodes = []
                for node in nodes_json["items"]:
                    conditions = {c["type"]: c["status"] for c in node["status"]["conditions"]}
                    if conditions.get("Ready") != "True":
                        not_ready_nodes.append(node["metadata"]["name"])

                if not not_ready_nodes:
                    log("OK", f"Cluster {cluster} is Ready ✅")
                    break
                else:
                    log("INFO", f"Cluster {cluster} not ready yet. Pending nodes: {', '.join(not_ready_nodes)}")

            except subprocess.CalledProcessError:
                log("WARN", f"Cluster {cluster} API not responding yet...")

            if time.time() - start > timeout:
                log("ERROR", f"Timeout waiting for cluster {cluster} ❌")
                break

            time.sleep(poll_interval)

            def wait_for_pods(cluster, namespace="--all-namespaces", timeout=300, poll_interval=5):
                """
                Wait until all pods in a cluster are running and ready.
                
                Parameters
                ----------
                cluster : str
                    The kind cluster name (without 'kind-' prefix).
                namespace : str
                    Namespace to check. Default is all namespaces.
                timeout : int
                    Maximum wait time in seconds.
                poll_interval : int
                    How often to poll (seconds).
                """
                log("INFO", f"Waiting for pods in cluster {cluster} to be healthy (timeout {timeout}s)...")
                start = time.time()

                while True:
                    try:
                        pods_json = json.loads(
                            run(f"kubectl get pods -o json {namespace} --context kind-{cluster}", capture=True)
                        )
                        not_ready_pods = []
                        for pod in pods_json["items"]:
                            name = pod["metadata"]["name"]
                            ns = pod["metadata"]["namespace"]
                            phase = pod["status"].get("phase", "Unknown")

                            # Pod must be in Running phase
                            if phase != "Running":
                                not_ready_pods.append(f"{ns}/{name} ({phase})")
                                continue

                            # All containers must be ready
                            statuses = pod["status"].get("containerStatuses", [])
                            if not all(s.get("ready", False) for s in statuses):
                                not_ready_pods.append(f"{ns}/{name} (containers not ready)")

                        if not not_ready_pods:
                            log("OK", f"All pods in {cluster} are healthy ✅")
                            return True
                        else:
                            log("INFO", f"Cluster {cluster} has pending pods: {', '.join(not_ready_pods[:5])}" +
                                (", ..." if len(not_ready_pods) > 5 else ""))

                    except subprocess.CalledProcessError:
                        log("WARN", f"Cluster {cluster} API not responding yet...")

                    if time.time() - start > timeout:
                        log("ERROR", f"Timeout waiting for pods in cluster {cluster} ❌")
                        return False

                    time.sleep(poll_interval)


def wait_for_pods(cluster, namespace="--all-namespaces", timeout=300, poll_interval=5):
    """
    Wait until all pods in a cluster are running and ready.
    
    Parameters
    ----------
    cluster : str
        The kind cluster name (without 'kind-' prefix).
    namespace : str
        Namespace to check. Default is all namespaces.
    timeout : int
        Maximum wait time in seconds.
    poll_interval : int
        How often to poll (seconds).
    """
    log("INFO", f"Waiting for pods in cluster {cluster} to be healthy (timeout {timeout}s)...")
    start = time.time()

    while True:
        try:
            pods_json = json.loads(
                run(f"kubectl get pods -o json {namespace} --context kind-{cluster}", capture=True)
            )
            not_ready_pods = []
            for pod in pods_json["items"]:
                name = pod["metadata"]["name"]
                ns = pod["metadata"]["namespace"]
                phase = pod["status"].get("phase", "Unknown")

                # Pod must be in Running phase
                if phase != "Running":
                    not_ready_pods.append(f"{ns}/{name} ({phase})")
                    continue

                # All containers must be ready
                statuses = pod["status"].get("containerStatuses", [])
                if not all(s.get("ready", False) for s in statuses):
                    not_ready_pods.append(f"{ns}/{name} (containers not ready)")

            if not not_ready_pods:
                log("OK", f"All pods in {cluster} are healthy ✅")
                return True
            else:
                log("INFO", f"Cluster {cluster} has pending pods: {', '.join(not_ready_pods[:5])}" +
                    (", ..." if len(not_ready_pods) > 5 else ""))

        except subprocess.CalledProcessError:
            log("WARN", f"Cluster {cluster} API not responding yet...")

        if time.time() - start > timeout:
            log("ERROR", f"Timeout waiting for pods in cluster {cluster} ❌")
            return False

        time.sleep(poll_interval)

def wait_for_resource_ready(resource, name=None, namespace="default", timeout=300, poll_interval=5):
    """
    Wait until a custom resource (or all of them) reports Ready status.
    Returns True if ready, False if timeout.
    """
    ns_arg = f"-n {namespace}" if namespace else ""
    target_desc = f"{resource}/{name}" if name else f"all {resource}"
    log("INFO", f"Waiting for {target_desc} to become Ready (timeout {timeout}s)...")
    start = time.time()

    while True:
        try:
            if name:
                cmd = f"kubectl get {resource} {name} {ns_arg} -o json"
                resources = [json.loads(run(cmd, capture=True))]
            else:
                cmd = f"kubectl get {resource} {ns_arg} -o json"
                resources = json.loads(run(cmd, capture=True)).get("items", [])

            if not resources:
                log("INFO", f"No {resource} found yet...")
            else:
                not_ready = []
                for res in resources:
                    rname = res["metadata"]["name"]
                    conditions = {c["type"]: c["status"] for c in res.get("status", {}).get("conditions", [])}

                    # Treat missing Ready condition as not ready
                    if conditions.get("Ready") != "True":
                        pending = [t for t, s in conditions.items() if s != "True"]
                        if not pending and "Ready" not in conditions:
                            pending = ["Ready (missing)"]
                        not_ready.append(f"{rname} (pending: {', '.join(pending)})")

                if not not_ready:
                    log("OK", f"{target_desc} is Ready ✅")
                    return True
                else:
                    log("INFO", f"Still waiting: {', '.join(not_ready[:5])}" +
                        (", ..." if len(not_ready) > 5 else ""))

        except subprocess.CalledProcessError:
            log("WARN", f"{target_desc} not found yet...")

        if time.time() - start > timeout:
            log("ERROR", f"Timeout waiting for {target_desc} ❌")
            return False

        time.sleep(poll_interval)



# ----------------------------
# Create interfaces for vlan
# ----------------------------

def create_vlan_interfaces(clusters_yaml=CLUSTERS_YAML):
    """
    Create VLAN interfaces on worker nodes for all clusters.

    This function is idempotent: running it multiple times will not cause errors
    if VLAN interfaces already exist.
    """
    try:
        vlan_indices = json.loads(
            run(f"kubectl get vlanindices.vlan.be.kuid.dev {TOPOLOGY_NAME} -o json", capture=True)
        )["status"]
        vlan_min = vlan_indices.get("minID")
        vlan_max = vlan_indices.get("maxID", vlan_min)
    except (KeyError, subprocess.CalledProcessError, json.JSONDecodeError):
        log("ERROR", "Failed to fetch VLAN index range")
        return

    log("INFO", f"Creating VLAN interfaces for VLAN IDs {vlan_min}–{vlan_max}")

    for cluster in clusters_yaml.keys():
        try:
            cmd = (
                f"kubectl get nodes "
                f"-l node-role.kubernetes.io/control-plane!= "
                f"-o jsonpath='{{range .items[*]}}{{.metadata.name}}{{\"\\n\"}}{{end}}' "
                f"--context kind-{cluster}"
            )
            worker_nodes = run(cmd, capture=True).splitlines()
        except subprocess.CalledProcessError:
            log("WARN", f"Failed to get worker nodes for {cluster}, skipping.")
            continue

        for worker in worker_nodes:
            for vlan_id in range(vlan_min, vlan_max + 1):
                iface = f"eth1.{vlan_id}"

                # Check if VLAN interface already exists
                try:
                    run(f"docker exec {worker} ip link show {iface}", check=True)
                    log("INFO", f"{worker}: {iface} already exists, skipping.")
                    continue
                except subprocess.CalledProcessError:
                    pass  # Interface does not exist → safe to create

                # Create VLAN interface
                try:
                    run(f"docker exec {worker} ip link add link eth1 name {iface} type vlan id {vlan_id}")
                    run(f"docker exec {worker} ip link set up {iface}")
                    log("OK", f"{worker}: created {iface}")
                except subprocess.CalledProcessError:
                    log("ERROR", f"Failed to create {iface} on {worker}")

# ----------------------------
# NETWORK CR GENERATION
# ----------------------------

def generate_network_crs(output_file="network-crs.yaml"):

    log("INFO", "Fetching VLAN and IP claims...")
    vlans={}
    ips={}
    intefaces=[json.loads(run("kubectl get vlanclaims.vlan.be.kuid.dev -o json", capture=True))["items"][i]["metadata"]["name"] for i in range(0,len(json.loads(run("kubectl get vlanclaims.vlan.be.kuid.dev -o json", capture=True))["items"]))]
    try:
        for i in intefaces:
            vlans.update({i: json.loads(run(f"kubectl get vlanclaims.vlan.be.kuid.dev -l nephio.org/network-name={i} -o json", capture=True))["items"][0]["status"]["id"]})
            ipclaim=json.loads(run(f"kubectl get ipclaims.ipam.be.kuid.dev -l nephio.org/network-name={i} -o json", capture=True))["items"]
            val=[]
            for c in range(0,len(ipclaim)):
                val.append({"node":ipclaim[c]["spec"]["selector"]["matchLabels"]["nephio.org/site"],"address":ipclaim[c]["status"]["address"]})
            ips.update({i: val})
    except subprocess.CalledProcessError:
        log("ERROR", "Failed to fetch VLAN or IP claims")
        return

    vpcs=[]
    # random network id this is used for interfaces
    network_id = []
    for i in intefaces:
        test={}
        bridge = []
        routing = []
        test={
              "apiVersion": "network.app.kuid.dev/v1alpha1",
              "kind": "Network",
              "metadata": {"name":f"5g.vpc-{i}"},
              "spec": {
                "topology": f"{TOPOLOGY_NAME}"
                }
            }
        for claim in ips[i]:
            tmp=[x for x in random.sample(range(1, 1000), 3) if x not in network_id]
            network_id.append(tmp)
            bridge.append({
                "name": f"{claim['node']}-bd",
                "networkID": tmp[0],
                "interfaces": [{
                "endpoint": ENDPOINT_BASE[claim["node"]],
                "node": DEFAULT_NODE,
                "region": DEFAULT_REGION,
                "site": DEFAULT_SITE,
                "vlanID": vlans[i]                
                }
                ]
                })
            routing.append({
                "name": f"{i}-rt",
                "networkID": random.randint(1,1000),
                "interfaces": [{
                "bridgeDomain":f"{claim['node']}-bd",
                "node": DEFAULT_NODE,
                "region": DEFAULT_REGION,
                "site": DEFAULT_SITE,
                "vlanID": vlans[i],
                "addresses": [{"address":claim["address"]}]                
                }
                ]
                })
        test["spec"].update({"bridgeDomains":bridge})
        test["spec"].update({"routingTables":routing})
        vpcs.append(test)

    if not vpcs:
        log("WARN", "No Network CRs generated (missing claims or mappings).")
        return

    with open(output_file, "w") as f:
        for vpc in vpcs:
            f.write("---\n")
            yaml.safe_dump(vpc, f, sort_keys=False)

    log("INFO", f"Generated {len(vpcs)} VPC Generated → {output_file}")

def apply_network_crs():
    """Apply the generated CRs to the current kube-context."""
    try:
        run(f"kubectl apply -f {OUTPUT_CRS_FILE}")
        log("OK", f"Applied {OUTPUT_CRS_FILE}")
        wait_for_resource_ready(resource="configs.config.sdcio.dev")
    except subprocess.CalledProcessError:
        log("ERROR", f"Failed to apply {OUTPUT_CRS_FILE}")

# ----------------------------
# ORCHESTRATION
# ----------------------------
def create_infra():
    log("INFO", "Setting sysctl limits")
    run("sudo sysctl -w fs.inotify.max_user_watches=524288")
    run("sudo sysctl -w fs.inotify.max_user_instances=512")
    run("sudo sysctl -w kernel.keys.maxkeys=500000")
    run("sudo sysctl -w kernel.keys.maxbytes=1000000")
    run("rm /tmp/vars.json || true", check=False)

    create_networks()
    for cluster in CLUSTERS_YAML.keys():
        create_kind_cluster(cluster)
    wait_for_clusters(300)

    # Write workers JSON
    workers = []
    for cluster in CLUSTERS_YAML.keys():
        out = run(
            f"kubectl get nodes -l node-role.kubernetes.io/control-plane!= -o jsonpath='{{.items[*].metadata.name}}' --context kind-{cluster}",
            capture=True,
        )
        if out:
            workers.extend(out.split())

    with open("/tmp/vars.json", "w") as f:
        json.dump({"workers": workers}, f)

    run("sudo containerlab deploy --topo clab-topo.gotmpl --vars /tmp/vars.json")
    log("OK", "Creating Kubenet Infra")
    run("kubectl config use-context kind-mgmt")
    run("kubectl apply -f infra-kubenet/pkgserver.yaml")
    run("kubectl apply -f infra-kubenet/sdc.yaml")
    run("kubectl apply -f infra-kubenet/kuid-server.yaml")
    run("kubectl apply -f infra-kubenet/kuid-nokia-srl.yaml")
    run("kubectl apply -f infra-kubenet/kuidapps.yaml")
    wait_for_pods("mgmt")
    log("OK", "Infra created ✅")

def destroy_infra():
    run("sudo containerlab destroy --topo clab-topo.gotmpl --vars /tmp/vars.json || true", check=False)
    run("rm /tmp/vars.json || true", check=False)
    run("sudo rm -rf clab-5g || true", check=False)
    delete_kind_clusters()
    delete_networks()

def status_infra():
    log("INFO", "Current clusters:")
    try:
        run("kind get clusters")
    except subprocess.CalledProcessError:
        log("WARN", "No clusters found")

    log("INFO", "Current docker networks:")
    for net, config in NETWORKS.items():
        bridge = config.split(":")[1]
        try:
            subnet = run(
                f"docker network inspect {bridge} -f '{{{{(index .IPAM.Config 0).Subnet}}}}'",
                capture=True,
            )
            print(f"  {bridge} ({subnet})")
        except subprocess.CalledProcessError:
            print(f"  {bridge} (not found)")

def create_network_plan(step=None):
    """Create network plan in multiple stages, selectable via CLI."""
    if step in (None, "discovery"):
        log("INFO", "Creating device definition:")
        run("kubectl apply -f network-plan/discovery.yaml")
        wait_for_resource_ready(resource="targets.inv.sdcio.dev")

    if step in (None, "inventory"):
        log("INFO", "Creating device inventory and registering CLAB topology:")
        run("kubectl apply -f network-plan/inventory.yaml")
        wait_for_resource_ready(resource="nodes.infra.be.kuid.dev")

    if step in (None, "vlan-indices"):
        log("INFO", "Creating VLAN Indices:")
        run("kubectl apply -f network-plan/vlan-indicies.yaml")
        wait_for_resource_ready(resource="vlanindices.vlan.be.kuid.dev")

    if step in (None, "dynamic-vlan"):
        log("INFO", "Requesting VLANs for different networks:")
        run("kubectl apply -f network-plan/dynamic-vlan.yaml")
        wait_for_resource_ready(resource="vlanclaims.vlan.be.kuid.dev")

    if step in (None, "ip-indices"):
        log("INFO", "Creating IP prefixes:")
        run("kubectl apply -f network-plan/ipindex.yaml")
        wait_for_resource_ready(resource="ipindices.ipam.be.kuid.dev")

    if step in (None, "networks"):
        log("INFO", "Creating different networks:")
        run("kubectl apply -f network-plan/network-config.yaml")
        wait_for_resource_ready(resource="ipclaims.ipam.be.kuid.dev")

    if step in (None, "ipclaims"):
        log("INFO", "Creating IP Claims for the VPCs:")
        run("kubectl apply -f network-plan/ipclaim-vpcs.yaml")
        wait_for_resource_ready(resource="ipclaims.ipam.be.kuid.dev")

    if step in (None, "vlan-interfaces"):
        log("INFO", "Creating VLANs on the worker nodes:")
        create_vlan_interfaces()

    log("OK", f"Network plan step '{step or 'all'}' completed ✅")
# ---------------------------
# HELPER
# ---------------------------

def print_help():
    help_text = """
Usage:
  infra-manager.py <command> [step]

Commands:
  create         Create infrastructure (networks, clusters, multus, containerlab, infra components)
  destroy        Destroy all infrastructure
  status         Show current clusters and docker networks
  network-plan   Run network plan workflow (all steps or a specific one)
  gen-crs        Generate Network Custom Resources (VLAN/IP claims network-crs.yaml)
  apply-crs      Apply generated Network CR to the current kube-context (mgmt)

Network-plan steps (optional):
  discovery        To discovery devices
  inventory        Create inventory & register container lab topology
  vlan-indices     Create VLAN index ranges
  dynamic-vlan     Request VLAN claims for different networks
  ip-indices       Create IP index ranges
  networks         Create network configs and IP claims
  ipclaims         Request IP claims VPC default gateway
  vlan-interfaces  Create VLAN interfaces on worker nodes

Examples:
  ./infra-manager.py create
  ./infra-manager.py status
  ./infra-manager.py network-plan discovery
  ./infra-manager.py gen-crs && ./infra-manager.py apply-crs
"""
    print(help_text.strip())

# ----------------------------
# MAIN
# ----------------------------
def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        print_help()
        sys.exit(0)

    cmd = sys.argv[1]
    step = sys.argv[2] if len(sys.argv) > 2 else None

    if cmd == "create":
        create_infra()
    elif cmd == "destroy":
        destroy_infra()
    elif cmd == "status":
        status_infra()
    elif cmd == "gen-crs":
        generate_network_crs()
    elif cmd == "apply-crs":
        apply_network_crs()
    elif cmd == "network-plan":
        create_network_plan(step=step)
    else:
        print(f"[ERROR] Unknown command: {cmd}\n")
        print_help()
        sys.exit(1)

if __name__ == "__main__":
    main()

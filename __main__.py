"""
Pulumi program to expose k3s applications to the public using Cloudflare Tunnel.

This program creates:
1. A Cloudflare Tunnel for secure connectivity
2. Tunnel configuration with ingress rules for each service
3. DNS CNAME records pointing to the tunnel
4. A Kubernetes Deployment running cloudflared in the k3s cluster
"""

import base64
import secrets

import pulumi
import pulumi_cloudflare as cloudflare
import pulumi_kubernetes as k8s

# Configuration
config = pulumi.Config()
cloudflare_config = pulumi.Config("cloudflare")

# Required configuration values
account_id = cloudflare_config.require("accountId")
zone_id = config.require("zoneId")
domain = config.require("domain")

# Services to expose via the tunnel
# Format: list of {"subdomain": "app", "service": "http://service:port"}
services = config.require_object("services")

# Optional: Kubernetes namespace for cloudflared deployment
k8s_namespace = config.get("k8sNamespace") or "cloudflare-tunnel"

# Generate a secure tunnel secret (32 bytes, base64 encoded)
tunnel_secret = config.get_secret("tunnelSecret") or pulumi.Output.secret(
    base64.b64encode(secrets.token_bytes(32)).decode("utf-8")
)

# Create the Cloudflare Tunnel
tunnel = cloudflare.ZeroTrustTunnelCloudflared(
    "k3s-tunnel",
    account_id=account_id,
    name="k3s-cluster-tunnel",
    config_src="cloudflare",  # Manage configuration via Cloudflare dashboard/API
    secret=tunnel_secret,
)

# Build ingress rules for the tunnel configuration
# Each service gets a hostname -> service mapping
ingress_rules: list[
    cloudflare.ZeroTrustTunnelCloudflaredConfigConfigIngressRuleArgs
] = []
for svc in services:
    subdomain = svc["subdomain"]
    service_url = svc["service"]
    hostname = f"{subdomain}.{domain}"

    ingress_rules.append(
        cloudflare.ZeroTrustTunnelCloudflaredConfigConfigIngressRuleArgs(
            hostname=hostname,
            service=service_url,
        )
    )

# Add catch-all rule (required by Cloudflare)
ingress_rules.append(
    cloudflare.ZeroTrustTunnelCloudflaredConfigConfigIngressRuleArgs(
        service="http_status:404",
    )
)

# Configure the tunnel with ingress rules
tunnel_config = cloudflare.ZeroTrustTunnelCloudflaredConfig(
    "k3s-tunnel-config",
    account_id=account_id,
    tunnel_id=tunnel.id,
    config=cloudflare.ZeroTrustTunnelCloudflaredConfigConfigArgs(
        ingress_rules=ingress_rules,
    ),
)

# Create DNS CNAME records for each service pointing to the tunnel
# The tunnel CNAME target is: <tunnel-id>.cfargotunnel.com
dns_records = []
for svc in services:
    subdomain = svc["subdomain"]
    record = cloudflare.Record(
        f"dns-{subdomain}",
        zone_id=zone_id,
        name=subdomain,
        type="CNAME",
        content=tunnel.id.apply(lambda tid: f"{tid}.cfargotunnel.com"),
        proxied=True,  # Enable Cloudflare proxy for security and performance
        ttl=1,  # Auto TTL when proxied
    )
    dns_records.append(record)

# -----------------------------------------------------------------------------
# Kubernetes Resources for cloudflared connector
# -----------------------------------------------------------------------------

# Create namespace for cloudflared
namespace = k8s.core.v1.Namespace(
    "cloudflare-tunnel-ns",
    metadata=k8s.meta.v1.ObjectMetaArgs(
        name=k8s_namespace,
    ),
)

# Create a secret containing the tunnel token
# The token is used by cloudflared to authenticate with Cloudflare
tunnel_token_secret = k8s.core.v1.Secret(
    "cloudflared-tunnel-token",
    metadata=k8s.meta.v1.ObjectMetaArgs(
        name="cloudflared-tunnel-token",
        namespace=namespace.metadata.name,
    ),
    type="Opaque",
    string_data={
        # The tunnel token is: base64(account_id:tunnel_id:tunnel_secret)
        "tunnel-token": pulumi.Output.all(tunnel.id, tunnel_secret).apply(
            lambda args: base64.b64encode(
                f"{account_id}:{args[0]}:{args[1]}".encode()
            ).decode()
        ),
    },
)

# Deploy cloudflared as a Deployment in the k3s cluster
cloudflared_deployment = k8s.apps.v1.Deployment(
    "cloudflared",
    metadata=k8s.meta.v1.ObjectMetaArgs(
        name="cloudflared",
        namespace=namespace.metadata.name,
        labels={"app": "cloudflared"},
    ),
    spec=k8s.apps.v1.DeploymentSpecArgs(
        replicas=2,  # Run 2 replicas for high availability
        selector=k8s.meta.v1.LabelSelectorArgs(
            match_labels={"app": "cloudflared"},
        ),
        template=k8s.core.v1.PodTemplateSpecArgs(
            metadata=k8s.meta.v1.ObjectMetaArgs(
                labels={"app": "cloudflared"},
            ),
            spec=k8s.core.v1.PodSpecArgs(
                containers=[
                    k8s.core.v1.ContainerArgs(
                        name="cloudflared",
                        image="cloudflare/cloudflared:latest",
                        args=[
                            "tunnel",
                            "--no-autoupdate",
                            "run",
                            "--token",
                            "$(TUNNEL_TOKEN)",
                        ],
                        env=[
                            k8s.core.v1.EnvVarArgs(
                                name="TUNNEL_TOKEN",
                                value_from=k8s.core.v1.EnvVarSourceArgs(
                                    secret_key_ref=k8s.core.v1.SecretKeySelectorArgs(
                                        name=tunnel_token_secret.metadata.name,
                                        key="tunnel-token",
                                    ),
                                ),
                            ),
                        ],
                        resources=k8s.core.v1.ResourceRequirementsArgs(
                            requests={
                                "cpu": "100m",
                                "memory": "128Mi",
                            },
                            limits={
                                "cpu": "500m",
                                "memory": "256Mi",
                            },
                        ),
                        liveness_probe=k8s.core.v1.ProbeArgs(
                            http_get=k8s.core.v1.HTTPGetActionArgs(
                                path="/ready",
                                port=2000,
                            ),
                            initial_delay_seconds=10,
                            period_seconds=10,
                        ),
                        readiness_probe=k8s.core.v1.ProbeArgs(
                            http_get=k8s.core.v1.HTTPGetActionArgs(
                                path="/ready",
                                port=2000,
                            ),
                            initial_delay_seconds=5,
                            period_seconds=5,
                        ),
                    ),
                ],
            ),
        ),
    ),
    opts=pulumi.ResourceOptions(depends_on=[tunnel_config]),
)

# Export tunnel information
pulumi.export("tunnelId", tunnel.id)
pulumi.export("tunnelName", tunnel.name)
pulumi.export(
    "dnsRecords", [r.name.apply(lambda n: f"{n}.{domain}") for r in dns_records]
)
pulumi.export("k8sNamespace", namespace.metadata.name)

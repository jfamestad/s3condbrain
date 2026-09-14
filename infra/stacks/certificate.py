"""Certificate stack — the one piece of infrastructure deployed by hand, never by CI.

A regional ACM certificate for ``cfg.domain`` (API Gateway REGIONAL endpoints need the
certificate in the API's own region). Its ARN is published to SSM at
``/wiki/<env>/certificate-arn``; the API stack reads that parameter at deploy time, so
nothing has to be pasted between stacks.

Why a separate stack, outside CI/CD:
* Issuance is a human act — DNS validation needs a CNAME placed at the DNS provider
  (there is no Route 53 zone for the domain), and CloudFormation waits, sometimes for
  the better part of an hour, until it resolves. A pipeline should not sit in that
  wait, and an automated redeploy should never be able to replace or delete a
  certificate the live domain is using.
* It changes on a different cadence from everything else: once, then at renewal
  (which ACM performs automatically while the CNAME remains in place).

Deployed with its own CDK app entry (``infra/cert_app.py``) via ``make cert-deploy``.
The default app (``infra/app.py``) never includes it, so ``make deploy`` and the CI
synth cannot touch it. The account guard applies exactly as it does to the main app.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import RemovalPolicy
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_route53 as route53
from aws_cdk import aws_ssm as ssm
from constructs import Construct

from infra.config import EnvConfig


def parameter_name(env_name: str) -> str:
    """SSM parameter holding the certificate ARN for an environment."""
    return f"/wiki/{env_name}/certificate-arn"


class CertificateStack(cdk.Stack):
    """One regional certificate for ``cfg.domain``, published to SSM.

    Args:
        cfg: The environment. ``domain`` is required. With ``hosted_zone_name`` the
            validation records are created automatically in that Route 53 zone;
            without it, CloudFormation pauses until the operator adds the CNAME that
            ``make cert-status`` prints.
    """

    def __init__(self, scope: Construct, id: str, *, cfg: EnvConfig, **kwargs) -> None:
        super().__init__(scope, id, **kwargs)
        self.cfg = cfg
        if not cfg.domain:
            raise ValueError(f"env {cfg.name!r} has no domain; there is nothing to certify")

        if cfg.hosted_zone_name:
            zone = route53.HostedZone.from_lookup(self, "Zone", domain_name=cfg.hosted_zone_name)
            validation = acm.CertificateValidation.from_dns(zone)
        else:
            # No zone: ACM still issues via DNS, but the CNAME is placed by a person.
            validation = acm.CertificateValidation.from_dns()

        self.certificate = acm.Certificate(
            self,
            "Certificate",
            domain_name=cfg.domain,
            validation=validation,
        )
        # Never let a stack deletion take the live domain's certificate with it.
        self.certificate.apply_removal_policy(RemovalPolicy.RETAIN)

        self.parameter = ssm.StringParameter(
            self,
            "CertificateArnParameter",
            parameter_name=parameter_name(cfg.name),
            string_value=self.certificate.certificate_arn,
            description=f"ACM certificate for {cfg.domain}; read by the wiki-{cfg.name}-api stack",
        )

        cdk.CfnOutput(self, "CertificateArn", value=self.certificate.certificate_arn)
        cdk.CfnOutput(self, "ParameterName", value=self.parameter.parameter_name)
        cdk.CfnOutput(
            self,
            "ValidationHint",
            value=(
                "If this deploy is waiting: `make cert-status ENV="
                f"{cfg.name}` prints the CNAME to add at your DNS provider."
            ),
        )


__all__ = ["CertificateStack", "parameter_name"]

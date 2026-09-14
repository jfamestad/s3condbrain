"""The hand-deployed certificate stack (infra/stacks/certificate.py, infra/cert_app.py)."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import aws_cdk as cdk
import pytest

from infra import cert_app
from infra.config import load
from infra.stacks.certificate import CertificateStack, parameter_name


def _synth(cfg: Any) -> dict[str, Any]:
    app = cdk.App()
    CertificateStack(
        app,
        f"wiki-{cfg.name}-certificate",
        cfg=cfg,
        env=cdk.Environment(account=cfg.account or "123456789012", region=cfg.region),
    )
    return app.synth().get_stack_by_name(f"wiki-{cfg.name}-certificate").template


def _only(template: dict[str, Any], type_: str) -> dict[str, Any]:
    found = [v for v in template["Resources"].values() if v["Type"] == type_]
    assert len(found) == 1, f"expected one {type_}, found {len(found)}"
    return found[0]


def test_issues_a_dns_validated_certificate_for_the_domain() -> None:
    cfg = load("dev")
    template = _synth(cfg)
    cert = _only(template, "AWS::CertificateManager::Certificate")
    assert cert["Properties"]["DomainName"] == cfg.domain
    assert cert["Properties"]["ValidationMethod"] == "DNS"
    assert cert.get("DeletionPolicy") == "Retain", "a stack delete must not take the live cert"
    assert cert.get("UpdateReplacePolicy") == "Retain"


def test_publishes_the_arn_to_the_parameter_the_api_reads() -> None:
    cfg = load("dev")
    template = _synth(cfg)
    param = _only(template, "AWS::SSM::Parameter")
    assert param["Properties"]["Name"] == parameter_name("dev") == "/wiki/dev/certificate-arn"
    cert_id = next(
        k
        for k, v in template["Resources"].items()
        if v["Type"] == "AWS::CertificateManager::Certificate"
    )
    assert param["Properties"]["Value"] == {"Ref": cert_id}
    outputs = template["Outputs"]
    assert {"CertificateArn", "ParameterName", "ValidationHint"} <= set(outputs)


def test_no_zone_means_no_route53_records_and_no_lookups() -> None:
    template = _synth(load("dev"))  # dev has hosted_zone_name=None
    assert not [v for v in template["Resources"].values() if v["Type"].startswith("AWS::Route53")]


def test_refuses_an_env_without_a_domain() -> None:
    with pytest.raises(ValueError, match="no domain"):
        _synth(replace(load("dev"), domain=None))


def test_cert_app_builds_only_the_certificate_stack(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CDK_DEFAULT_ACCOUNT", raising=False)
    app = cdk.App(context={"env": "dev"})
    cfg = cert_app.build(app)
    assembly = app.synth()
    names = sorted(s.stack_name for s in assembly.stacks)
    assert names == ["wiki-dev-certificate"]
    environment = assembly.get_stack_by_name("wiki-dev-certificate").environment
    assert (environment.account, environment.region) == (cfg.account, cfg.region)


def test_cert_app_applies_the_account_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CDK_DEFAULT_ACCOUNT", "999999999999")
    with pytest.raises(SystemExit):
        cert_app.build(cdk.App(context={"env": "dev"}))


def test_main_app_never_includes_the_certificate_stack(tmp_path: Any) -> None:
    from infra.app import build as build_main

    app = cdk.App(context={"env": "dev", "codeRoot": str(tmp_path)})
    build_main(app)
    names = {s.stack_name for s in app.synth().stacks}
    assert "wiki-dev-certificate" not in names
    assert not any("certificate" in n for n in names)

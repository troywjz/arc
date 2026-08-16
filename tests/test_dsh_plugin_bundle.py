from __future__ import annotations

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "package.json"
ADAPTER = ROOT / "plugins/arc/dsh/index.js"
PATCH = ROOT / "plugins/arc/dsh/cordis.patch.yml"
SKILL = ROOT / "plugins/arc/skills/arc/SKILL.md"
BRIDGE_TEST = ROOT / "tests/test_dsh_llm_bridge.mjs"


def test_dsh_bundle_manifest_points_to_adapter_patch() -> None:
    manifest = json.loads(PACKAGE.read_text(encoding="utf-8"))
    assert manifest["name"] == "dsh-arc"
    assert manifest["main"] == "./plugins/arc/dsh/index.js"
    assert manifest["dsh"]["bundle"]["patch"] == (
        "./plugins/arc/dsh/cordis.patch.yml"
    )
    assert manifest["bin"]["arc-paper"] == "./plugins/arc/bin/arc-paper"
    assert manifest["bin"]["arc-runtime"] == "./plugins/arc/bin/arc-runtime"
    assert "dsh-plugin" in manifest["keywords"]


def test_dsh_patch_loads_package_entry() -> None:
    patch = PATCH.read_text(encoding="utf-8")
    assert "id: arc" in patch
    assert "name: dsh-arc" in patch


def test_dsh_adapter_registers_existing_arc_skill() -> None:
    script = """
      import { apply } from './plugins/arc/dsh/index.js'
      let captured
      let cleanup
      apply({
        skills: { register(value) { captured = value } },
        llm: { prepareCall() { throw new Error('not called by registration test') } },
        get() { return undefined },
        effect(callback) { cleanup = callback(); return () => {} },
      })
      if (captured?.name !== 'arc') process.exit(1)
      if (captured?.resourceBase?.kind !== 'directory') process.exit(2)
      if (!captured?.content?.includes('# Agent Research Copilot')) process.exit(3)
      if (!captured?.content?.includes('arc-runtime')) process.exit(4)
      await cleanup?.()
    """
    subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        cwd=ROOT,
        check=True,
    )


def test_arc_skill_and_runtime_resources_are_present() -> None:
    assert SKILL.is_file()
    assert (SKILL.parent / "scripts/arc-runtime").is_file()
    assert (SKILL.parent / "manuals/arc-paper.md").is_file()
    assert (SKILL.parent / "rules/integrity.md").is_file()


def test_dsh_native_bridge_protocol_smoke() -> None:
    subprocess.run(["node", str(BRIDGE_TEST)], cwd=ROOT, check=True)

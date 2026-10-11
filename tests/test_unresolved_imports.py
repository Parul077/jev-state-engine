import json
import subprocess
from pathlib import Path
import pytest

from jev.models import Subgoal
from jev.workspace import Workspace


@pytest.fixture
def repo_workspace(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "tester@example.com"], cwd=repo, check=True, capture_output=True)

    # Initial commit
    (repo / "init.txt").write_text("initial", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)

    # Base node_modules in repo root
    (repo / "node_modules").mkdir(exist_ok=True)

    return Workspace(repo_dir=repo)


# =========================================================================
# Case 1: Missing package through each import form
# =========================================================================

def test_missing_import_from_fails(repo_workspace):
    """import ... from 'missing' must fail with failed_check='unresolved_import'."""
    ws = repo_workspace
    subgoal = Subgoal(description="Add file", scope=["src/index.ts"], expects_tests=False)

    f = ws.repo_dir / "src" / "index.ts"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("import { util } from 'missing-pkg';\nexport const x = 1;\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/index.ts"], cwd=ws.repo_dir, check=True, capture_output=True)

    res = ws.run_mechanical_checks(subgoal)
    assert res.passed is False
    assert res.failed_check == "unresolved_import"
    assert "missing-pkg" in res.detail


def test_missing_dynamic_import_fails(repo_workspace):
    """import('missing') must fail with failed_check='unresolved_import'."""
    ws = repo_workspace
    subgoal = Subgoal(description="Add file", scope=["src/index.ts"], expects_tests=False)

    f = ws.repo_dir / "src" / "index.ts"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("async function load() { const mod = await import('missing-dyn'); }\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/index.ts"], cwd=ws.repo_dir, check=True, capture_output=True)

    res = ws.run_mechanical_checks(subgoal)
    assert res.passed is False
    assert res.failed_check == "unresolved_import"
    assert "missing-dyn" in res.detail


def test_missing_require_fails(repo_workspace):
    """require('missing') must fail with failed_check='unresolved_import'."""
    ws = repo_workspace
    subgoal = Subgoal(description="Add file", scope=["src/index.js"], expects_tests=False)

    f = ws.repo_dir / "src" / "index.js"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("const lib = require('missing-req');\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/index.js"], cwd=ws.repo_dir, check=True, capture_output=True)

    res = ws.run_mechanical_checks(subgoal)
    assert res.passed is False
    assert res.failed_check == "unresolved_import"
    assert "missing-req" in res.detail


def test_missing_export_from_fails(repo_workspace):
    """export ... from 'missing' must fail with failed_check='unresolved_import'."""
    ws = repo_workspace
    subgoal = Subgoal(description="Add file", scope=["src/index.tsx"], expects_tests=False)

    f = ws.repo_dir / "src" / "index.tsx"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("export { widget } from 'missing-exp';\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/index.tsx"], cwd=ws.repo_dir, check=True, capture_output=True)

    res = ws.run_mechanical_checks(subgoal)
    assert res.passed is False
    assert res.failed_check == "unresolved_import"
    assert "missing-exp" in res.detail


def test_missing_side_effect_import_fails(repo_workspace):
    """import 'missing' (side-effect import) must fail with failed_check='unresolved_import'."""
    ws = repo_workspace
    subgoal = Subgoal(description="Add file", scope=["src/index.ts"], expects_tests=False)

    f = ws.repo_dir / "src" / "index.ts"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("import 'missing-side-effect';\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/index.ts"], cwd=ws.repo_dir, check=True, capture_output=True)

    res = ws.run_mechanical_checks(subgoal)
    assert res.passed is False
    assert res.failed_check == "unresolved_import"
    assert "missing-side-effect" in res.detail


# =========================================================================
# Case 2: Installed package passes
# =========================================================================

def test_installed_package_passes(repo_workspace):
    """Installed package in node_modules must pass."""
    ws = repo_workspace
    subgoal = Subgoal(description="Add file", scope=["src/index.ts"], expects_tests=False)

    # Fake node_modules in repo root
    pkg_dir = ws.repo_dir / "node_modules" / "installed-pkg"
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / "package.json").write_text('{"name": "installed-pkg"}', encoding="utf-8")

    f = ws.repo_dir / "src" / "index.ts"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("import { util } from 'installed-pkg';\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/index.ts"], cwd=ws.repo_dir, check=True, capture_output=True)

    res = ws.run_mechanical_checks(subgoal)
    assert res.failed_check != "unresolved_import"


# =========================================================================
# Case 3: Relative import passes
# =========================================================================

def test_relative_import_passes(repo_workspace):
    """Relative imports (./ and ../) must pass."""
    ws = repo_workspace
    subgoal = Subgoal(description="Add file", scope=["src/index.ts"], expects_tests=False)

    f = ws.repo_dir / "src" / "index.ts"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("import { a } from './local';\nimport { b } from '../parent';\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/index.ts"], cwd=ws.repo_dir, check=True, capture_output=True)

    res = ws.run_mechanical_checks(subgoal)
    assert res.failed_check != "unresolved_import"


# =========================================================================
# Case 4: Node built-in modules pass
# =========================================================================

def test_node_builtins_pass(repo_workspace):
    """node:fs, fs, path, etc. must pass."""
    ws = repo_workspace
    subgoal = Subgoal(description="Add file", scope=["src/index.ts"], expects_tests=False)

    f = ws.repo_dir / "src" / "index.ts"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("import fs from 'node:fs';\nimport path from 'path';\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/index.ts"], cwd=ws.repo_dir, check=True, capture_output=True)

    res = ws.run_mechanical_checks(subgoal)
    assert res.failed_check != "unresolved_import"


# =========================================================================
# Case 5: @scope/pkg/sub/path reduces correctly
# =========================================================================

def test_scoped_package_reduces_correctly(repo_workspace):
    """@scope/pkg/sub/path must reduce to @scope/pkg."""
    ws = repo_workspace
    subgoal = Subgoal(description="Add file", scope=["src/index.ts"], expects_tests=False)

    f = ws.repo_dir / "src" / "index.ts"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("import sub from '@scope/pkg/sub/path';\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/index.ts"], cwd=ws.repo_dir, check=True, capture_output=True)

    # When missing, fails mentioning @scope/pkg
    res = ws.run_mechanical_checks(subgoal)
    assert res.passed is False
    assert res.failed_check == "unresolved_import"
    assert "@scope/pkg" in res.detail

    # When installed in node_modules, passes
    scoped_dir = ws.repo_dir / "node_modules" / "@scope" / "pkg"
    scoped_dir.mkdir(parents=True, exist_ok=True)
    (scoped_dir / "package.json").write_text('{"name": "@scope/pkg"}', encoding="utf-8")

    res2 = ws.run_mechanical_checks(subgoal)
    assert res2.failed_check != "unresolved_import"


# =========================================================================
# Case 6: next/image passes when next is installed
# =========================================================================

def test_next_image_passes_when_next_is_installed(repo_workspace):
    """next/image reduces to next and passes when next is in node_modules."""
    ws = repo_workspace
    subgoal = Subgoal(description="Add file", scope=["src/index.tsx"], expects_tests=False)

    next_dir = ws.repo_dir / "node_modules" / "next"
    next_dir.mkdir(parents=True, exist_ok=True)

    f = ws.repo_dir / "src" / "index.tsx"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("import Image from 'next/image';\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/index.tsx"], cwd=ws.repo_dir, check=True, capture_output=True)

    res = ws.run_mechanical_checks(subgoal)
    assert res.failed_check != "unresolved_import"


# =========================================================================
# Case 7: @/x passes with tsconfig alias
# =========================================================================

def test_tsconfig_path_alias_passes(repo_workspace):
    """tsconfig.json compilerOptions.paths alias (e.g. @/*) must be skipped."""
    ws = repo_workspace
    subgoal = Subgoal(description="Add file", scope=["src/index.ts"], expects_tests=False)

    tsconfig = ws.repo_dir / "tsconfig.json"
    tsconfig.write_text(json.dumps({
        "compilerOptions": {
            "baseUrl": ".",
            "paths": {
                "@/*": ["src/*"]
            }
        }
    }), encoding="utf-8")

    f = ws.repo_dir / "src" / "index.ts"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("import Header from '@/components/Header';\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=ws.repo_dir, check=True, capture_output=True)

    res = ws.run_mechanical_checks(subgoal)
    assert res.failed_check != "unresolved_import"


# =========================================================================
# Case 8: Pre-existing import in context lines passes
# =========================================================================

def test_preexisting_import_in_context_passes(repo_workspace):
    """Pre-existing uninstalled import in context lines must pass."""
    ws = repo_workspace
    # Commit old file with missing import
    f = ws.repo_dir / "src" / "old.ts"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("import missing from 'preexisting-missing';\nconst a = 1;\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/old.ts"], cwd=ws.repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "old commit"], cwd=ws.repo_dir, check=True, capture_output=True)

    # Now edit without adding any new uninstalled import
    subgoal = Subgoal(description="Edit old", scope=["src/old.ts"], expects_tests=False)
    f.write_text("import missing from 'preexisting-missing';\nconst a = 2;\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/old.ts"], cwd=ws.repo_dir, check=True, capture_output=True)

    res = ws.run_mechanical_checks(subgoal)
    assert res.failed_check != "unresolved_import"


# =========================================================================
# Case 9: Commented-out import passes
# =========================================================================

def test_commented_out_import_passes(repo_workspace):
    """Commented-out imports (// or /* ... */) must not trigger unresolved_import."""
    ws = repo_workspace
    subgoal = Subgoal(description="Add file", scope=["src/index.ts"], expects_tests=False)

    f = ws.repo_dir / "src" / "index.ts"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(
        "// import missing from 'missing-single-line';\n"
        "/* import missing2 from 'missing-block'; */\n"
        "export const ok = 1;\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "src/index.ts"], cwd=ws.repo_dir, check=True, capture_output=True)

    res = ws.run_mechanical_checks(subgoal)
    assert res.failed_check != "unresolved_import"


# =========================================================================
# Case 10: declare module 'missing' fails & declare module 'installed' passes
# =========================================================================

def test_declare_module_missing_fails(repo_workspace):
    """declare module 'missing' with bare unresolvable name fails."""
    ws = repo_workspace
    subgoal = Subgoal(description="Add types", scope=["src/types.d.ts"], expects_tests=False)

    f = ws.repo_dir / "src" / "types.d.ts"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("declare module 'missing-type-pkg' { export const x: any; }\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/types.d.ts"], cwd=ws.repo_dir, check=True, capture_output=True)

    res = ws.run_mechanical_checks(subgoal)
    assert res.passed is False
    assert res.failed_check == "unresolved_import"
    assert "missing-type-pkg" in res.detail


def test_declare_module_installed_passes(repo_workspace):
    """declare module 'installed' for an installed package passes."""
    ws = repo_workspace
    subgoal = Subgoal(description="Augment types", scope=["src/types.d.ts"], expects_tests=False)

    pkg_dir = ws.repo_dir / "node_modules" / "installed-type-pkg"
    pkg_dir.mkdir(parents=True, exist_ok=True)

    f = ws.repo_dir / "src" / "types.d.ts"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("declare module 'installed-type-pkg' { export const custom: string; }\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/types.d.ts"], cwd=ws.repo_dir, check=True, capture_output=True)

    res = ws.run_mechanical_checks(subgoal)
    assert res.failed_check != "unresolved_import"


# =========================================================================
# Case 11: Part A gaps in check_suppression
# =========================================================================

def test_suppression_plus_plus_syntax_detected(repo_workspace):
    """Added line starting with ++ (e.g. ++i; // @ts-ignore) must trigger suppression gate."""
    ws = repo_workspace
    subgoal = Subgoal(description="Increment", scope=["src/counter.ts"], expects_tests=False)

    f = ws.repo_dir / "src" / "counter.ts"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(
        "let i = 0;\n"
        "++i; // @ts-ignore\n"
        "export { i };\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "src/counter.ts"], cwd=ws.repo_dir, check=True, capture_output=True)

    res = ws.run_mechanical_checks(subgoal)
    assert res.passed is False
    assert res.failed_check == "suppression"
    assert "@ts-ignore" in res.detail


def test_suppression_mts_and_vue_files(repo_workspace):
    """.mts and .vue files must also be checked for suppression directives."""
    ws = repo_workspace
    subgoal = Subgoal(description="Add modules", scope=["src/a.mts", "src/b.vue"], expects_tests=False)

    f_mts = ws.repo_dir / "src" / "a.mts"
    f_mts.parent.mkdir(parents=True, exist_ok=True)
    f_mts.write_text("// @ts-ignore\nexport const a = 1;\n", encoding="utf-8")

    f_vue = ws.repo_dir / "src" / "b.vue"
    f_vue.write_text("<script>\n// @ts-nocheck\n</script>\n", encoding="utf-8")

    subprocess.run(["git", "add", "src/a.mts", "src/b.vue"], cwd=ws.repo_dir, check=True, capture_output=True)

    res = ws.run_mechanical_checks(subgoal)
    assert res.passed is False
    assert res.failed_check == "suppression"


# =========================================================================
# Case 12: Feedback text format requirement
# =========================================================================

def test_unresolved_import_feedback_message_format(repo_workspace):
    """Feedback message must include: Package '<pkg>' is imported in <file> at line <n> but is not installed (declared in package.json: yes/no). Do not import packages that are not installed. Use only installed packages, or escalate if the ticket requires a package that does not exist."""
    ws = repo_workspace

    # Subcase 1: declared in package.json: yes
    (ws.repo_dir / "package.json").write_text(json.dumps({
        "dependencies": {
            "declared-pkg": "^1.0.0"
        }
    }), encoding="utf-8")
    subprocess.run(["git", "add", "package.json"], cwd=ws.repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add package.json"], cwd=ws.repo_dir, check=True, capture_output=True)

    subgoal1 = Subgoal(description="Import declared", scope=["src/comp1.ts"], expects_tests=False)
    f1 = ws.repo_dir / "src" / "comp1.ts"
    f1.parent.mkdir(parents=True, exist_ok=True)
    f1.write_text("import x from 'declared-pkg';\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/comp1.ts"], cwd=ws.repo_dir, check=True, capture_output=True)

    res1 = ws.run_mechanical_checks(subgoal1)
    assert res1.passed is False
    assert res1.failed_check == "unresolved_import"
    expected_msg1 = (
        "Package 'declared-pkg' is imported in src/comp1.ts at line 1 but is not installed "
        "(declared in package.json: yes). Do not import packages that are not installed. "
        "Use only installed packages, or escalate if the ticket requires a package that does not exist."
    )
    assert expected_msg1 in res1.detail

    # Clean up comp1.ts before subcase 2
    subprocess.run(["git", "reset", "HEAD", "--", "."], cwd=ws.repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "checkout", "--", "."], cwd=ws.repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "clean", "-fd"], cwd=ws.repo_dir, check=True, capture_output=True)
    (ws.repo_dir / "node_modules").mkdir(exist_ok=True)

    # Subcase 2: declared in package.json: no
    subgoal2 = Subgoal(description="Import undeclared", scope=["src/comp2.ts"], expects_tests=False)
    f2 = ws.repo_dir / "src" / "comp2.ts"
    f2.parent.mkdir(parents=True, exist_ok=True)
    f2.write_text("import y from 'undeclared-pkg';\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/comp2.ts"], cwd=ws.repo_dir, check=True, capture_output=True)

    res2 = ws.run_mechanical_checks(subgoal2)
    assert res2.passed is False
    assert res2.failed_check == "unresolved_import"
    expected_msg2 = (
        "Package 'undeclared-pkg' is imported in src/comp2.ts at line 1 but is not installed "
        "(declared in package.json: no). Do not import packages that are not installed. "
        "Use only installed packages, or escalate if the ticket requires a package that does not exist."
    )
    assert expected_msg2 in res2.detail


# =========================================================================
# Case 13: Non-npm grammar specifiers pass
# =========================================================================

@pytest.mark.parametrize("specifier", [
    "#internal/x",
    "bun:test",
    "virtual:pwa-register",
    "~/lib/x",
    "$lib/x",
])
def test_non_npm_grammar_specifiers_pass(repo_workspace, specifier):
    """Specifiers that don't match npm package name grammar (#internal, bun:, virtual:, ~, $) must be skipped."""
    ws = repo_workspace
    subgoal = Subgoal(description="Add file", scope=["src/index.ts"], expects_tests=False)

    f = ws.repo_dir / "src" / "index.ts"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(f"import foo from '{specifier}';\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/index.ts"], cwd=ws.repo_dir, check=True, capture_output=True)

    res = ws.run_mechanical_checks(subgoal)
    assert res.failed_check != "unresolved_import"


# =========================================================================
# Case 14: Alias defined in parent config reached through extends passes
# =========================================================================

def test_extends_parent_tsconfig_alias_passes(repo_workspace):
    """Path alias defined in a parent config reached through extends must pass."""
    ws = repo_workspace
    subgoal = Subgoal(description="Add file", scope=["src/index.ts"], expects_tests=False)

    # Parent config
    base_config = ws.repo_dir / "tsconfig.base.json"
    base_config.write_text(json.dumps({
        "compilerOptions": {
            "paths": {
                "@parent-alias/*": ["src/parent/*"]
            }
        }
    }), encoding="utf-8")

    # Child config extending parent
    child_config = ws.repo_dir / "tsconfig.json"
    child_config.write_text(json.dumps({
        "extends": "./tsconfig.base"
    }), encoding="utf-8")

    f = ws.repo_dir / "src" / "index.ts"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("import something from '@parent-alias/utils';\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=ws.repo_dir, check=True, capture_output=True)

    res = ws.run_mechanical_checks(subgoal)
    assert res.failed_check != "unresolved_import"


# =========================================================================
# Case 15: baseUrl-relative import passes
# =========================================================================

def test_base_url_relative_import_passes(repo_workspace):
    """If baseUrl is set, a specifier whose first segment exists as a file or directory under baseUrl must pass."""
    ws = repo_workspace
    subgoal = Subgoal(description="Add file", scope=["src/index.ts"], expects_tests=False)

    tsconfig = ws.repo_dir / "tsconfig.json"
    tsconfig.write_text(json.dumps({
        "compilerOptions": {
            "baseUrl": "./src"
        }
    }), encoding="utf-8")

    # Create a directory and file under src/
    helpers_dir = ws.repo_dir / "src" / "helpers"
    helpers_dir.mkdir(parents=True, exist_ok=True)
    (helpers_dir / "math.ts").write_text("export const add = 1;\n", encoding="utf-8")

    f = ws.repo_dir / "src" / "index.ts"
    f.write_text("import { add } from 'helpers/math';\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=ws.repo_dir, check=True, capture_output=True)

    res = ws.run_mechanical_checks(subgoal)
    assert res.failed_check != "unresolved_import"


# =========================================================================
# Case 16: No node_modules anywhere -> import 'react' passes this check
# =========================================================================

def test_no_node_modules_anywhere_passes_unresolved_import(tmp_path):
    """If no node_modules exists anywhere in the resolution chain, check_unresolved_imports returns passed."""
    repo = tmp_path / "repo_no_nm"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "tester@example.com"], cwd=repo, check=True, capture_output=True)

    (repo / "init.txt").write_text("initial", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)

    ws = Workspace(repo_dir=repo)
    assert not (repo / "node_modules").exists()

    subgoal = Subgoal(description="Add react import", scope=["src/App.tsx"], expects_tests=False)
    f = ws.repo_dir / "src" / "App.tsx"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("import React from 'react';\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/App.tsx"], cwd=ws.repo_dir, check=True, capture_output=True)

    res = ws.run_mechanical_checks(subgoal)
    assert res.failed_check != "unresolved_import"
    assert res.checks["unresolved_import"]["passed"] is True


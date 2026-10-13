import json
import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from jev.ledger import compute_repo_key, get_jev_home
from jev.repo_profile import (
    ECOSYSTEM_FRAMEWORKS,
    EcosystemProfile,
    RepositoryProfile,
    build_repo_profile,
    compute_manifest_hash,
    get_or_build_repo_profile,
)
from jev.workspace import ECOSYSTEM_COMPILE_HANDLERS, JsTsCompileHandler


def test_real_jev_home_untouched(tmp_path):
    """Asserts that tests operate in isolated tmp JEV_HOME and real ~/.jev is untouched."""
    real_jev = Path.home() / ".jev"
    initial_exists = real_jev.exists()
    initial_mtime = real_jev.stat().st_mtime if initial_exists else None

    # Run profile building on tmp repo
    repo = tmp_path / "dummy_repo"
    repo.mkdir()
    (repo / "package.json").write_text('{"name": "test"}')

    profile = get_or_build_repo_profile(repo, use_cache=True)
    assert profile is not None

    if not initial_exists:
        assert not real_jev.exists()
    else:
        assert real_jev.stat().st_mtime == initial_mtime


def test_js_fixtures_lockfiles(tmp_path):
    """Tests JS with npm, pnpm, yarn, and bun lockfiles."""
    # 1. npm
    repo_npm = tmp_path / "repo_npm"
    repo_npm.mkdir()
    (repo_npm / "package.json").write_text(
        json.dumps({
            "name": "app-npm",
            "type": "module",
            "dependencies": {"react": "^18.0.0"},
        })
    )
    (repo_npm / "package-lock.json").write_text("{}")
    prof_npm = build_repo_profile(repo_npm)
    assert len(prof_npm.ecosystems) == 1
    eco_npm = prof_npm.ecosystems[0]
    assert eco_npm.ecosystem == "js_ts"
    assert eco_npm.package_manager == "npm"
    assert eco_npm.module_system == "esm"
    assert eco_npm.frameworks == ["react"]
    assert prof_npm.ecosystems[0].test_command is None
    assert "tests: none" in prof_npm.to_prompt_text()

    # 2. pnpm
    repo_pnpm = tmp_path / "repo_pnpm"
    repo_pnpm.mkdir()
    (repo_pnpm / "package.json").write_text(
        json.dumps({
            "name": "app-pnpm",
            "dependencies": {"vue": "^3.0.0"},
        })
    )
    (repo_pnpm / "pnpm-lock.yaml").write_text("")
    prof_pnpm = build_repo_profile(repo_pnpm)
    assert prof_pnpm.ecosystems[0].package_manager == "pnpm"
    assert prof_pnpm.ecosystems[0].module_system == "cjs"
    assert prof_pnpm.ecosystems[0].frameworks == ["vue"]

    # 3. yarn
    repo_yarn = tmp_path / "repo_yarn"
    repo_yarn.mkdir()
    (repo_yarn / "package.json").write_text(
        json.dumps({
            "name": "app-yarn",
            "dependencies": {"svelte": "^4.0.0"},
        })
    )
    (repo_yarn / "yarn.lock").write_text("")
    prof_yarn = build_repo_profile(repo_yarn)
    assert prof_yarn.ecosystems[0].package_manager == "yarn"
    assert prof_yarn.ecosystems[0].frameworks == ["svelte"]

    # 4. bun
    repo_bun = tmp_path / "repo_bun"
    repo_bun.mkdir()
    (repo_bun / "package.json").write_text(
        json.dumps({
            "name": "app-bun",
            "dependencies": {"express": "^4.18.0"},
        })
    )
    (repo_bun / "bun.lockb").write_text("")
    prof_bun = build_repo_profile(repo_bun)
    assert prof_bun.ecosystems[0].package_manager == "bun"
    assert prof_bun.ecosystems[0].frameworks == ["express"]


def test_python_fixtures_poetry_and_pip(tmp_path):
    """Tests Python poetry and pip package managers and frameworks."""
    # 1. Poetry
    repo_poetry = tmp_path / "repo_poetry"
    repo_poetry.mkdir()
    (repo_poetry / "pyproject.toml").write_text(
        """[tool.poetry]
name = "poetry-app"
[tool.poetry.dependencies]
python = "^3.11"
fastapi = "^0.100.0"
pytest = "^7.0.0"
"""
    )
    (repo_poetry / "poetry.lock").write_text("")
    prof_poetry = build_repo_profile(repo_poetry)
    assert len(prof_poetry.ecosystems) == 1
    eco_poetry = prof_poetry.ecosystems[0]
    assert eco_poetry.ecosystem == "python"
    assert eco_poetry.package_manager == "poetry"
    assert eco_poetry.module_system is None
    assert eco_poetry.compile_status == "syntax_check_only"
    assert eco_poetry.compile_command is None
    assert eco_poetry.frameworks == ["fastapi", "pytest"]

    # 2. Pip
    repo_pip = tmp_path / "repo_pip"
    repo_pip.mkdir()
    (repo_pip / "requirements.txt").write_text("django>=4.2\nflask==2.3.0\n")
    prof_pip = build_repo_profile(repo_pip)
    assert len(prof_pip.ecosystems) == 1
    eco_pip = prof_pip.ecosystems[0]
    assert eco_pip.ecosystem == "python"
    assert eco_pip.package_manager == "pip"
    assert eco_pip.frameworks == ["django", "flask"]


def test_rust_fixture(tmp_path):
    """Tests Rust Cargo.toml profile."""
    repo = tmp_path / "repo_rust"
    repo.mkdir()
    (repo / "Cargo.toml").write_text(
        """[package]
name = "rust-app"
version = "0.1.0"
[dependencies]
tokio = "1"
axum = "0.6"
"""
    )
    prof = build_repo_profile(repo)
    assert len(prof.ecosystems) == 1
    eco = prof.ecosystems[0]
    assert eco.ecosystem == "rust"
    assert eco.package_manager == "cargo"
    assert eco.manifest == "Cargo.toml"
    assert eco.compile_command == ["cargo", "check"]
    assert eco.compile_status == "command"
    assert eco.frameworks == ["axum", "tokio"]


def test_go_fixture(tmp_path):
    """Tests Go go.mod profile."""
    repo = tmp_path / "repo_go"
    repo.mkdir()
    (repo / "go.mod").write_text(
        """module example.com/mygoapp

go 1.20

require (
    github.com/gin-gonic/gin v1.9.1
    github.com/labstack/echo v4.11.0
)
"""
    )
    prof = build_repo_profile(repo)
    assert len(prof.ecosystems) == 1
    eco = prof.ecosystems[0]
    assert eco.ecosystem == "go"
    assert eco.package_manager == "go"
    assert eco.manifest == "go.mod"
    assert eco.compile_command == ["go", "build", "./..."]
    assert eco.compile_status == "command"
    assert eco.frameworks == ["gin-gonic/gin", "labstack/echo"]


def test_maven_fixture(tmp_path):
    """Tests Maven pom.xml profile."""
    repo = tmp_path / "repo_mvn"
    repo.mkdir()
    (repo / "pom.xml").write_text(
        """<project>
    <dependencies>
        <dependency>
            <groupId>org.springframework.boot</groupId>
            <artifactId>spring-boot-starter-web</artifactId>
        </dependency>
    </dependencies>
</project>
"""
    )
    prof = build_repo_profile(repo)
    assert len(prof.ecosystems) == 1
    eco = prof.ecosystems[0]
    assert eco.ecosystem == "maven"
    assert eco.package_manager == "mvn"
    assert eco.manifest == "pom.xml"
    assert eco.compile_status == "command"
    assert eco.compile_command == ["mvn", "-q", "compile"]
    assert eco.frameworks == ["spring-boot"]


def test_gradle_fixture(tmp_path):
    """Tests Gradle build.gradle.kts profile."""
    repo = tmp_path / "repo_gradle"
    repo.mkdir()
    (repo / "build.gradle.kts").write_text(
        """dependencies {
    implementation("org.springframework.boot:spring-boot-starter:3.0.0")
}
"""
    )
    prof = build_repo_profile(repo)
    assert len(prof.ecosystems) == 1
    eco = prof.ecosystems[0]
    assert eco.ecosystem == "gradle"
    assert eco.package_manager == "gradle"
    assert eco.manifest == "build.gradle.kts"
    assert eco.compile_status == "command"
    assert eco.compile_command == ["gradle", "classes", "-q"]
    assert eco.frameworks == ["spring-boot"]


def test_monorepo_fixture(tmp_path):
    """Tests monorepo with apps/web (JS) plus root Cargo.toml."""
    repo = tmp_path / "monorepo"
    repo.mkdir()
    (repo / "Cargo.toml").write_text(
        """[package]
name = "core"
version = "0.1.0"
[dependencies]
tokio = "1"
"""
    )
    web = repo / "apps" / "web"
    web.mkdir(parents=True)
    (web / "package.json").write_text(
        json.dumps({
            "name": "web",
            "scripts": {"typecheck": "tsc", "test": "jest"},
            "dependencies": {"next": "14.0.0", "react": "18.2.0"},
        })
    )
    (web / "package-lock.json").write_text("{}")
    (web / "node_modules").mkdir()

    prof = build_repo_profile(repo)
    assert len(prof.ecosystems) == 2
    # Sorted by root, ecosystem: "." comes before "apps/web"
    eco_rust = prof.ecosystems[0]
    assert eco_rust.root == "."
    assert eco_rust.ecosystem == "rust"
    assert eco_rust.compile_status == "command"
    assert eco_rust.compile_command == ["cargo", "check"]

    eco_js = prof.ecosystems[1]
    assert eco_js.root == "apps/web"
    assert eco_js.ecosystem == "js_ts"
    assert eco_js.package_manager == "npm"
    assert eco_js.compile_status == "not_gated"
    assert eco_js.compile_command is None
    assert eco_js.test_command is None
    assert eco_js.test_source is None
    assert eco_js.frameworks == ["next", "react"]

    prompt_text = prof.to_prompt_text()
    assert "not run by the gate" in prompt_text
    assert "js_ts @ apps/web: npm, cjs; not run by the gate; frameworks: next, react" in prompt_text


def test_pytest_interpreter_prompt_text(tmp_path):
    """Tests that to_prompt_text() prints python -m pytest -q while stored test_command is unchanged."""
    import sys
    repo = tmp_path / "py_pytest"
    repo.mkdir()
    (repo / "pyproject.toml").write_text('[project]\nname="my-pkg"\n')
    (repo / "pytest.ini").write_text("[pytest]\n")

    prof = build_repo_profile(repo)
    assert len(prof.ecosystems) == 1
    eco = prof.ecosystems[0]
    assert eco.test_command == [sys.executable, "-m", "pytest", "-q"]

    prompt_text = prof.to_prompt_text()
    assert "tests: python -m pytest -q" in prompt_text
    assert sys.executable not in prompt_text


def test_compile_command_equals_handler_return(tmp_path):
    """Asserts compile_command equals what the handler returns and handler method was called."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "Cargo.toml").write_text('[package]\nname="test"\n')

    rust_handler = next(h for h in ECOSYSTEM_COMPILE_HANDLERS if getattr(h, "name", None) == "rust")
    with patch.object(rust_handler, "prepare_and_get_command", wraps=rust_handler.prepare_and_get_command) as spy:
        prof = build_repo_profile(repo)
        assert spy.called
        args, kwargs = spy.call_args
        assert args[0] == repo
        assert args[1] == repo
        assert prof.ecosystems[0].compile_command == ["cargo", "check"]


def test_node_modules_missing_vs_present(tmp_path):
    """Asserts node_modules missing -> 'env_not_ready'; present -> 'command'."""
    repo = tmp_path / "repo_nm"
    repo.mkdir()
    (repo / "package.json").write_text(
        json.dumps({
            "name": "my-app",
            "scripts": {"typecheck": "tsc --noEmit"},
        })
    )

    # 1. Missing node_modules
    prof1 = build_repo_profile(repo)
    assert prof1.ecosystems[0].compile_status == "env_not_ready"
    assert prof1.ecosystems[0].compile_command is None

    # 2. Present node_modules
    nm = repo / "node_modules"
    nm.mkdir()
    prof2 = build_repo_profile(repo)
    assert prof2.ecosystems[0].compile_status == "command"
    assert prof2.ecosystems[0].compile_command == ["npm", "run", "typecheck"]


def test_python_repo_syntax_check_only(tmp_path):
    """Asserts Python repo produces syntax_check_only compile status and compile_command is None."""
    repo = tmp_path / "repo_py"
    repo.mkdir()
    (repo / "pyproject.toml").write_text('[project]\nname="py-pkg"\n')
    prof = build_repo_profile(repo)
    assert len(prof.ecosystems) == 1
    assert prof.ecosystems[0].compile_status == "syntax_check_only"
    assert prof.ecosystems[0].compile_command is None


def test_malformed_package_json_does_not_raise(tmp_path):
    """Asserts malformed package.json does not raise; ecosystem is listed with empty frameworks."""
    repo = tmp_path / "repo_malformed"
    repo.mkdir()
    (repo / "package.json").write_text("{ this is invalid json !!!")

    prof = build_repo_profile(repo)
    assert len(prof.ecosystems) == 1
    eco = prof.ecosystems[0]
    assert eco.ecosystem == "js_ts"
    assert eco.frameworks == []
    assert eco.manifest == "package.json"


def test_caching_and_invalidation(tmp_path):
    """Asserts byte-identical JSON on repeat build, cache hit does not call builder, editing rebuilds."""
    repo = tmp_path / "repo_cache"
    repo.mkdir()
    # Init git repo so compute_repo_key has a commit
    subprocess.run(["git", "init"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(repo), check=True)

    pkg_json = repo / "package.json"
    pkg_json.write_text(json.dumps({"name": "cached-app", "scripts": {"typecheck": "tsc"}}))
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), check=True, capture_output=True)

    # 1. First build
    prof1 = get_or_build_repo_profile(repo, use_cache=True)
    json1 = prof1.model_dump_json(indent=2)

    # 2. Second build -> cache hit, builder not called
    with patch("jev.repo_profile.build_repo_profile") as spy_builder:
        prof2 = get_or_build_repo_profile(repo, use_cache=True)
        assert not spy_builder.called
        json2 = prof2.model_dump_json(indent=2)
        assert json1 == json2

    # 3. Edit package.json -> cache invalidation
    pkg_json.write_text(json.dumps({"name": "cached-app-modified", "scripts": {"typecheck": "tsc"}}))
    with patch("jev.repo_profile.build_repo_profile", wraps=build_repo_profile) as spy_builder:
        prof3 = get_or_build_repo_profile(repo, use_cache=True)
        assert spy_builder.called
        assert prof3.manifest_hash != prof1.manifest_hash

    # 4. Adding node_modules -> cache invalidation
    nm = repo / "node_modules"
    nm.mkdir()
    with patch("jev.repo_profile.build_repo_profile", wraps=build_repo_profile) as spy_builder:
        prof4 = get_or_build_repo_profile(repo, use_cache=True)
        assert spy_builder.called
        assert prof4.manifest_hash != prof3.manifest_hash
        assert prof4.ecosystems[0].compile_status == "command"


def test_repo_dir_listing_unchanged_and_no_subprocess_during_build(tmp_path):
    """Asserts repo_dir listing is identical before/after (no writes) and build_repo_profile calls no subprocess."""
    repo = tmp_path / "repo_readonly"
    repo.mkdir()
    (repo / "Cargo.toml").write_text('[package]\nname="readonly"\n')
    (repo / "src").mkdir()
    (repo / "src" / "main.rs").write_text('fn main() {}')

    # Snapshot directory listing
    def get_listing(p: Path):
        return sorted([str(f.relative_to(p)) for f in p.rglob("*")])

    before_listing = get_listing(repo)

    # Assert build_repo_profile calls no subprocess
    with patch("subprocess.run") as mock_subproc_run, patch("subprocess.Popen") as mock_subproc_popen:
        prof = build_repo_profile(repo)
        assert not mock_subproc_run.called
        assert not mock_subproc_popen.called

    after_listing = get_listing(repo)
    assert before_listing == after_listing
    assert prof is not None


def test_nextjs_typescript_to_prompt_text(tmp_path, capsys):
    """A Next.js+TypeScript fixture: to_prompt_text() under 1200 chars; prints char count & len/4 token estimate."""
    repo = tmp_path / "next_app"
    repo.mkdir()
    (repo / "package.json").write_text(
        json.dumps({
            "name": "my-next-app",
            "type": "module",
            "scripts": {
                "typecheck": "tsc --noEmit",
                "test": "vitest run",
            },
            "dependencies": {
                "next": "^14.1.0",
                "react": "^18.2.0",
                "tailwindcss": "^3.4.1",
            },
            "devDependencies": {
                "typescript": "^5.3.3",
                "vitest": "^1.3.1",
            },
        })
    )
    (repo / "tsconfig.json").write_text(
        json.dumps({
            "compilerOptions": {
                "module": "esnext",
                "target": "es2022",
            }
        })
    )
    (repo / "package-lock.json").write_text("{}")
    (repo / "node_modules").mkdir()

    prof = build_repo_profile(repo)
    prompt_text = prof.to_prompt_text()

    char_count = len(prompt_text)
    token_estimate = char_count / 4.0

    print(f"\n[Next.js+TS Prompt Text]\n{prompt_text}")
    print(f"Character count: {char_count}")
    print(f"Token estimate (len/4): {token_estimate}")

    assert char_count < 1200
    assert "js_ts @ .: npm, esm, ts:esnext" in prompt_text
    assert "compile: npm run typecheck" in prompt_text
    assert "tests: npm test" in prompt_text
    assert "frameworks: next, react, tailwindcss, typescript, vitest" in prompt_text

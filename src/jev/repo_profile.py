from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Literal, Optional, Set, Tuple, Union

try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib  # type: ignore
    except ImportError:
        tomllib = None

from pydantic import BaseModel, ConfigDict, Field

from jev.ledger import compute_repo_key, get_jev_home
from jev.models import CompileOutcome
from jev.workspace import (
    ECOSYSTEM_COMPILE_HANDLERS,
    resolve_test_command,
)

# Table-driven frameworks per ecosystem
ECOSYSTEM_FRAMEWORKS: Dict[str, List[str]] = {
    "js_ts": [
        "next",
        "react",
        "vue",
        "svelte",
        "@sveltejs/kit",
        "nuxt",
        "@angular/core",
        "express",
        "fastify",
        "@nestjs/core",
        "vite",
        "vitest",
        "jest",
        "tailwindcss",
        "typescript",
    ],
    "python": [
        "django",
        "flask",
        "fastapi",
        "pytest",
    ],
    "go": [
        "gin-gonic/gin",
        "labstack/echo",
        "gofiber/fiber",
    ],
    "rust": [
        "tokio",
        "axum",
        "actix-web",
        "rocket",
    ],
    "maven": [
        "spring-boot",
    ],
    "gradle": [
        "spring-boot",
    ],
}

MANIFEST_CANDIDATES: List[Tuple[str, List[str]]] = [
    ("js_ts", ["package.json"]),
    ("python", ["pyproject.toml", "requirements.txt", "setup.py", "setup.cfg"]),
    ("go", ["go.mod"]),
    ("rust", ["Cargo.toml"]),
    ("maven", ["pom.xml"]),
    ("gradle", ["build.gradle", "build.gradle.kts"]),
]

SAMPLE_FILES: Dict[str, str] = {
    "js_ts": "sample.ts",
    "python": "sample.py",
    "go": "sample.go",
    "rust": "sample.rs",
    "maven": "sample.java",
    "gradle": "sample.java",
}

SKIP_DIRS: Set[str] = {
    "node_modules",
    ".git",
    "venv",
    ".venv",
    "dist",
    "build",
    "target",
    ".next",
    "__pycache__",
}

ALL_MANIFEST_FILENAMES: Set[str] = {
    "package.json",
    "pyproject.toml",
    "requirements.txt",
    "setup.py",
    "setup.cfg",
    "go.mod",
    "Cargo.toml",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "bun.lockb",
    "bun.lock",
    "poetry.lock",
    "uv.lock",
    "Pipfile.lock",
    "tsconfig.json",
}

TEST_ECOSYSTEM_MAP: Dict[str, str] = {
    "yarn": "js_ts",
    "pnpm": "js_ts",
    "npm": "js_ts",
    "package_json": "js_ts",
    "go": "go",
    "cargo": "rust",
    "maven": "maven",
    "gradle": "gradle",
    "pytest": "python",
}


class EcosystemProfile(BaseModel):
    model_config = ConfigDict(frozen=True)

    ecosystem: Literal["js_ts", "python", "go", "rust", "maven", "gradle"]
    root: str
    manifest: str
    package_manager: str
    module_system: Optional[str] = None
    compile_command: Optional[List[str]] = None
    compile_status: Literal["command", "no_compile_command", "env_not_ready", "syntax_check_only", "not_gated"]
    test_command: Optional[List[str]] = None
    test_source: Optional[str] = None
    frameworks: List[str] = Field(default_factory=list)


def _is_absolute_path_str(s: str) -> bool:
    if not s:
        return False
    if bool(re.match(r"^[a-zA-Z]:[\\/]", s)):
        return True
    if s.startswith("/") or s.startswith("\\\\"):
        return True
    return False


def _clean_executable_name(s: str) -> str:
    name = re.split(r"[\\/]", s)[-1]
    for ext in (".cmd", ".exe", ".bat"):
        if name.lower().endswith(ext):
            name = name[:-len(ext)]
            break
    return name


def _format_display_command(cmd: Optional[List[str]]) -> str:
    if not cmd:
        return "none"
    display_cmd = list(cmd)
    first = display_cmd[0]
    if (
        first == sys.executable
        or os.path.normcase(first) == os.path.normcase(sys.executable)
    ):
        display_cmd[0] = "python"
    elif _is_absolute_path_str(first):
        display_cmd[0] = _clean_executable_name(first)
    return " ".join(display_cmd)


class RepositoryProfile(BaseModel):
    model_config = ConfigDict(frozen=True)

    ecosystems: List[EcosystemProfile] = Field(default_factory=list)
    manifest_hash: str

    def to_prompt_text(self) -> str:
        lines = []
        for eco in self.ecosystems:
            prefix = f"{eco.ecosystem} @ {eco.root}: "
            if eco.module_system:
                pm_mod = f"{eco.package_manager}, {eco.module_system}"
            else:
                pm_mod = eco.package_manager

            if eco.frameworks:
                frameworks_part = ", ".join(eco.frameworks)
            else:
                frameworks_part = "none"

            if eco.compile_status == "not_gated":
                lines.append(f"{prefix}{pm_mod}; not run by the gate; frameworks: {frameworks_part}")
                continue

            if eco.compile_command:
                compile_part = _format_display_command(eco.compile_command)
            elif eco.compile_status == "syntax_check_only":
                compile_part = "syntax_check_only"
            elif eco.compile_status == "env_not_ready":
                compile_part = "env_not_ready"
            else:
                compile_part = "none"

            if eco.test_command:
                test_part = _format_display_command(eco.test_command)
            else:
                test_part = "none"

            lines.append(f"{prefix}{pm_mod}; compile: {compile_part}; tests: {test_part}; frameworks: {frameworks_part}")
        return "\n".join(lines)


def _scan_roots(repo_dir: Path) -> List[Path]:
    roots = [repo_dir]
    try:
        depth1 = [p for p in repo_dir.iterdir() if p.is_dir() and p.name not in SKIP_DIRS and not p.name.startswith(".")]
    except Exception:
        depth1 = []

    for d1 in depth1:
        roots.append(d1)
        try:
            depth2 = [p for p in d1.iterdir() if p.is_dir() and p.name not in SKIP_DIRS and not p.name.startswith(".")]
            roots.extend(depth2)
        except Exception:
            pass

    return sorted(roots, key=lambda p: (len(p.relative_to(repo_dir).parts), p.relative_to(repo_dir).as_posix()))


def _detect_js_pm(root: Path) -> str:
    if (root / "yarn.lock").exists():
        return "yarn"
    if (root / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (root / "bun.lockb").exists() or (root / "bun.lock").exists():
        return "bun"
    if (root / "package-lock.json").exists():
        return "npm"
    return "npm"


def _detect_python_pm(root: Path) -> str:
    if (root / "poetry.lock").exists():
        return "poetry"
    if (root / "uv.lock").exists():
        return "uv"
    if (root / "Pipfile.lock").exists():
        return "pipenv"
    return "pip"


def _detect_js_module_system(root: Path) -> str:
    mod = "cjs"
    pkg_path = root / "package.json"
    if pkg_path.exists():
        try:
            pkg_data = json.loads(pkg_path.read_text(encoding="utf-8", errors="replace"))
            if isinstance(pkg_data, dict):
                t = pkg_data.get("type")
                if t == "module":
                    mod = "esm"
                elif t == "commonjs":
                    mod = "cjs"
        except Exception:
            pass

    tsconfig_path = root / "tsconfig.json"
    if tsconfig_path.exists():
        try:
            content = tsconfig_path.read_text(encoding="utf-8", errors="replace")
            cleaned = re.sub(r"//.*?\n|/\*.*?\*/", "", content, flags=re.S)
            cleaned = re.sub(r",\s*([}\]])", r"\1", cleaned)
            ts_data = json.loads(cleaned)
            if isinstance(ts_data, dict):
                co = ts_data.get("compilerOptions") or {}
                if isinstance(co, dict) and "module" in co:
                    ts_mod = str(co["module"]).lower()
                    mod = f"{mod}, ts:{ts_mod}"
        except Exception:
            pass

    return mod


def _detect_frameworks(ecosystem: str, root: Path) -> List[str]:
    matched: Set[str] = set()
    if ecosystem == "js_ts":
        pkg_path = root / "package.json"
        if pkg_path.exists():
            try:
                pkg = json.loads(pkg_path.read_text(encoding="utf-8", errors="replace"))
                if isinstance(pkg, dict):
                    deps = pkg.get("dependencies") or {}
                    dev_deps = pkg.get("devDependencies") or {}
                    all_deps = set(deps.keys() if isinstance(deps, dict) else []).union(
                        set(dev_deps.keys() if isinstance(dev_deps, dict) else [])
                    )
                    for fw in ECOSYSTEM_FRAMEWORKS["js_ts"]:
                        if fw in all_deps:
                            matched.add(fw)
            except Exception:
                pass

    elif ecosystem == "python":
        targets = ECOSYSTEM_FRAMEWORKS["python"]
        pyproj = root / "pyproject.toml"
        if pyproj.exists():
            content = pyproj.read_text(encoding="utf-8", errors="replace")
            parsed = None
            if tomllib is not None:
                try:
                    parsed = tomllib.loads(content)
                except Exception:
                    parsed = None
            if parsed and isinstance(parsed, dict):
                proj_deps = parsed.get("project", {}).get("dependencies") or []
                if isinstance(proj_deps, list):
                    for dep in proj_deps:
                        name = re.split(r"[><=~!;\s\[]", str(dep))[0].lower().replace("_", "-")
                        if name in targets:
                            matched.add(name)
                opt_deps = parsed.get("project", {}).get("optional-dependencies") or {}
                if isinstance(opt_deps, dict):
                    for dep_list in opt_deps.values():
                        if isinstance(dep_list, list):
                            for dep in dep_list:
                                name = re.split(r"[><=~!;\s\[]", str(dep))[0].lower().replace("_", "-")
                                if name in targets:
                                    matched.add(name)
                poetry_deps = parsed.get("tool", {}).get("poetry", {}).get("dependencies") or {}
                if isinstance(poetry_deps, dict):
                    for name in poetry_deps.keys():
                        clean_name = str(name).lower().replace("_", "-")
                        if clean_name in targets:
                            matched.add(clean_name)
                groups = parsed.get("tool", {}).get("poetry", {}).get("group") or {}
                if isinstance(groups, dict):
                    for g in groups.values():
                        if isinstance(g, dict) and "dependencies" in g:
                            g_deps = g["dependencies"]
                            if isinstance(g_deps, dict):
                                for name in g_deps.keys():
                                    clean_name = str(name).lower().replace("_", "-")
                                    if clean_name in targets:
                                        matched.add(clean_name)
            else:
                for fw in targets:
                    if re.search(rf"(?i)(?:^|[\"'\s,;\[]){re.escape(fw)}(?:$|[><=~!\"'\s,;\]])", content):
                        matched.add(fw)

        for req_path in root.glob("requirements*.txt"):
            try:
                req_text = req_path.read_text(encoding="utf-8", errors="replace")
                for line in req_text.splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    name = re.split(r"[><=~!;\s\[]", line)[0].lower().replace("_", "-")
                    if name in targets:
                        matched.add(name)
            except Exception:
                pass

        for s_file in [root / "setup.py", root / "setup.cfg"]:
            if s_file.exists():
                try:
                    s_text = s_file.read_text(encoding="utf-8", errors="replace")
                    for fw in targets:
                        if re.search(rf"(?i)(?:^|[\"'\s,;\[]){re.escape(fw)}(?:$|[><=~!\"'\s,;\]])", s_text):
                            matched.add(fw)
                except Exception:
                    pass

    elif ecosystem == "go":
        mod_path = root / "go.mod"
        if mod_path.exists():
            try:
                content = mod_path.read_text(encoding="utf-8", errors="replace")
                for fw in ECOSYSTEM_FRAMEWORKS["go"]:
                    if fw in content:
                        matched.add(fw)
            except Exception:
                pass

    elif ecosystem == "rust":
        cargo_path = root / "Cargo.toml"
        if cargo_path.exists():
            try:
                content = cargo_path.read_text(encoding="utf-8", errors="replace")
                parsed = None
                if tomllib is not None:
                    try:
                        parsed = tomllib.loads(content)
                    except Exception:
                        parsed = None
                if parsed and isinstance(parsed, dict):
                    dep_keys = set()
                    for key in ["dependencies", "dev-dependencies", "build-dependencies"]:
                        val = parsed.get(key)
                        if isinstance(val, dict):
                            dep_keys.update(val.keys())
                    target_dict = parsed.get("target")
                    if isinstance(target_dict, dict):
                        for t in target_dict.values():
                            if isinstance(t, dict):
                                for key in ["dependencies", "dev-dependencies", "build-dependencies"]:
                                    val = t.get(key)
                                    if isinstance(val, dict):
                                        dep_keys.update(val.keys())
                    for fw in ECOSYSTEM_FRAMEWORKS["rust"]:
                        if fw in dep_keys:
                            matched.add(fw)
                else:
                    for fw in ECOSYSTEM_FRAMEWORKS["rust"]:
                        if re.search(rf"(?m)^\s*{re.escape(fw)}\s*=", content):
                            matched.add(fw)
            except Exception:
                pass

    elif ecosystem in ("maven", "gradle"):
        content = ""
        if ecosystem == "maven":
            pom = root / "pom.xml"
            if pom.exists():
                try:
                    content = pom.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    pass
        else:
            for g_file in [root / "build.gradle", root / "build.gradle.kts"]:
                if g_file.exists():
                    try:
                        content += "\n" + g_file.read_text(encoding="utf-8", errors="replace")
                    except Exception:
                        pass
        if "spring-boot" in content.lower():
            matched.add("spring-boot")

    return sorted(matched)


def _detect_compile(ecosystem: str, root: Path) -> Tuple[Optional[List[str]], str]:
    handler = next((h for h in ECOSYSTEM_COMPILE_HANDLERS if getattr(h, "name", None) == ecosystem), None)
    if handler is None:
        return None, "no_compile_command"

    sample_file = SAMPLE_FILES.get(ecosystem, "sample.txt")
    try:
        cmd, outcome, msg = handler.prepare_and_get_command(root, root, [sample_file])
    except Exception:
        return None, "no_compile_command"

    if cmd == ["ast.parse"]:
        return None, "syntax_check_only"
    elif outcome == CompileOutcome.NO_COMPILE_COMMAND:
        return None, "no_compile_command"
    elif outcome == CompileOutcome.ENV_NOT_READY:
        return None, "env_not_ready"
    elif cmd is not None:
        return cmd, "command"
    else:
        return None, "no_compile_command"


def _detect_test(ecosystem: str, root: Path) -> Tuple[Optional[List[str]], Optional[str]]:
    try:
        resolved = resolve_test_command(root)
    except Exception:
        return None, None

    mapped_eco = TEST_ECOSYSTEM_MAP.get(resolved.ecosystem)
    if mapped_eco == ecosystem and resolved.command is not None:
        return resolved.command, resolved.source
    return None, None


def compute_manifest_hash(repo_dir: Union[str, Path]) -> str:
    repo_path = Path(repo_dir).resolve()
    roots = _scan_roots(repo_path)
    entries: List[Tuple[str, str]] = []

    for r in roots:
        rel_r = "." if r == repo_path else r.relative_to(repo_path).as_posix()
        for check_dir in ["node_modules", ".venv", "venv"]:
            exists_str = "1" if (r / check_dir).exists() else "0"
            entries.append((f"{rel_r}/{check_dir}:exists", exists_str))

        for fname in ALL_MANIFEST_FILENAMES:
            fpath = r / fname
            if fpath.is_file():
                rel_file = fpath.relative_to(repo_path).as_posix()
                try:
                    content_hash = hashlib.sha256(fpath.read_bytes()).hexdigest()
                    entries.append((rel_file, content_hash))
                except Exception:
                    pass

    entries.sort(key=lambda x: x[0])
    combined = "\n".join(f"{k}:{v}" for k, v in entries).encode("utf-8")
    return hashlib.sha256(combined).hexdigest()


def build_repo_profile(repo_dir: Union[str, Path]) -> RepositoryProfile:
    repo_path = Path(repo_dir).resolve()
    roots = _scan_roots(repo_path)
    ecosystems: List[EcosystemProfile] = []

    for r in roots:
        rel_r = "." if r == repo_path else r.relative_to(repo_path).as_posix()
        for eco_name, candidates in MANIFEST_CANDIDATES:
            found_manifest = None
            for c in candidates:
                if (r / c).is_file():
                    found_manifest = c
                    break
            if not found_manifest:
                continue

            if eco_name == "js_ts":
                pm = _detect_js_pm(r)
                module_system = _detect_js_module_system(r)
            elif eco_name == "python":
                pm = _detect_python_pm(r)
                module_system = None
            elif eco_name == "go":
                pm = "go"
                module_system = None
            elif eco_name == "rust":
                pm = "cargo"
                module_system = None
            elif eco_name == "maven":
                pm = "mvn"
                module_system = None
            elif eco_name == "gradle":
                pm = "gradle"
                module_system = None
            else:
                pm = "unknown"
                module_system = None

            if rel_r != ".":
                compile_cmd = None
                compile_status = "not_gated"
                test_cmd = None
                test_source = None
            else:
                compile_cmd, compile_status = _detect_compile(eco_name, r)
                test_cmd, test_source = _detect_test(eco_name, r)
            frameworks = _detect_frameworks(eco_name, r)

            eco_profile = EcosystemProfile(
                ecosystem=eco_name,
                root=rel_r,
                manifest=found_manifest,
                package_manager=pm,
                module_system=module_system,
                compile_command=compile_cmd,
                compile_status=compile_status,
                test_command=test_cmd,
                test_source=test_source,
                frameworks=frameworks,
            )
            ecosystems.append(eco_profile)

    ecosystems.sort(key=lambda e: (e.root, e.ecosystem))
    manifest_hash = compute_manifest_hash(repo_path)

    return RepositoryProfile(
        ecosystems=ecosystems,
        manifest_hash=manifest_hash,
    )


def get_or_build_repo_profile(
    repo_dir: Union[str, Path], use_cache: bool = True
) -> RepositoryProfile:
    repo_path = Path(repo_dir).resolve()
    repo_key = compute_repo_key(repo_path)
    cache_file = get_jev_home() / "repos" / repo_key / "profile.json"

    if use_cache and cache_file.exists():
        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            cached = RepositoryProfile.model_validate(data)
            current_hash = compute_manifest_hash(repo_path)
            if cached.manifest_hash == current_hash:
                return cached
        except Exception:
            pass

    profile = build_repo_profile(repo_path)
    if use_cache:
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(profile.model_dump_json(indent=2), encoding="utf-8")
        except Exception:
            pass

    return profile

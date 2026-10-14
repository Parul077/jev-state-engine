import builtins
import subprocess
from unittest.mock import patch
import pytest

from jev.dependency_delta import (
    DepChange,
    DependencyDelta,
    compute_dependency_delta,
)


def test_package_json_add_remove_version_bump_sections():
    old_pkg = """{
      "dependencies": {
        "react": "17.0.0",
        "lodash": "4.17.20"
      },
      "devDependencies": {
        "typescript": "4.9.0"
      }
    }"""
    new_pkg = """{
      "dependencies": {
        "react": "18.2.0",
        "axios": "1.4.0"
      },
      "devDependencies": {
        "typescript": "4.9.0",
        "jest": "29.0.0"
      },
      "peerDependencies": {
        "react-dom": "18.2.0"
      }
    }"""
    delta = compute_dependency_delta({"package.json": (old_pkg, new_pkg)})

    assert not delta.is_empty
    assert delta.unparsed == []

    # added: axios (dependencies), jest (devDependencies), react-dom (peerDependencies)
    added_names = [(c.name, c.section, c.new_version, c.ecosystem) for c in delta.added]
    assert ("axios", "dependencies", "1.4.0", "js_ts") in added_names
    assert ("jest", "devDependencies", "29.0.0", "js_ts") in added_names
    assert ("react-dom", "peerDependencies", "18.2.0", "js_ts") in added_names

    # removed: lodash (dependencies)
    removed_names = [(c.name, c.section, c.old_version, c.ecosystem) for c in delta.removed]
    assert ("lodash", "dependencies", "4.17.20", "js_ts") in removed_names

    # changed: react 17.0.0 -> 18.2.0
    changed_names = [(c.name, c.section, c.old_version, c.new_version, c.ecosystem) for c in delta.changed]
    assert ("react", "dependencies", "17.0.0", "18.2.0", "js_ts") in changed_names


def test_pyproject_toml_project_and_poetry():
    old_toml = """
[project]
dependencies = [
    "requests>=2.28.0",
    "flask==2.2.0",
]

[project.optional-dependencies]
test = [
    "pytest>=7.0.0",
]

[tool.poetry.dependencies]
python = "^3.11"
httpx = "^0.24.0"

[tool.poetry.group.dev.dependencies]
black = "^23.1.0"
"""
    new_toml = """
[project]
dependencies = [
    "requests>=2.31.0",
    "fastapi>=0.100.0",
]

[project.optional-dependencies]
test = [
    "pytest>=7.4.0",
    "pytest-cov>=4.0.0",
]

[tool.poetry.dependencies]
python = "^3.11"
httpx = "^0.25.0"

[tool.poetry.group.dev.dependencies]
ruff = "^0.1.0"
"""
    delta = compute_dependency_delta({"pyproject.toml": (old_toml, new_toml)})

    assert not delta.is_empty
    assert delta.unparsed == []

    # Python entry in poetry dependencies should be ignored
    all_names = [c.name for c in delta.added + delta.removed + delta.changed]
    assert "python" not in all_names

    # Added
    added_map = {c.name: c for c in delta.added}
    assert "fastapi" in added_map
    assert "pytest-cov" in added_map
    assert "ruff" in added_map
    assert added_map["fastapi"].ecosystem == "python"

    # Removed
    removed_map = {c.name: c for c in delta.removed}
    assert "flask" in removed_map
    assert "black" in removed_map

    # Changed
    changed_map = {c.name: c for c in delta.changed}
    assert "requests" in changed_map
    assert changed_map["requests"].old_version == ">=2.28.0"
    assert changed_map["requests"].new_version == ">=2.31.0"
    assert "pytest" in changed_map
    assert "httpx" in changed_map


def test_requirements_txt_normalization_comments_options():
    old_req = """
# Base dependencies
Requests >= 2.28.0
Flask_Login == 0.6.0
Django_Cors_Headers ~= 3.0
-r base.txt
-i https://pypi.org/simple
--extra-index-url https://custom.pypi/simple
https://github.com/psf/requests/archive/master.zip
"""
    new_req = """
# Updated dependencies
requests >= 2.31.0
flask-login == 0.6.2
PyYAML
pytest >= 7.0.0 # inline comment
-r base.txt
"""
    delta = compute_dependency_delta({"requirements.txt": (old_req, new_req)})

    assert not delta.is_empty
    assert delta.unparsed == []

    # Normalized names: _ becomes -, lowercase
    changed_map = {c.name: c for c in delta.changed}
    assert "requests" in changed_map
    assert "flask-login" in changed_map
    assert changed_map["flask-login"].old_version == "== 0.6.0"
    assert changed_map["flask-login"].new_version == "== 0.6.2"

    removed_map = {c.name: c for c in delta.removed}
    assert "django-cors-headers" in removed_map

    added_map = {c.name: c for c in delta.added}
    assert "pyyaml" in added_map
    assert added_map["pyyaml"].new_version is None
    assert "pytest" in added_map


def test_requirements_dev_txt_matching():
    old_req = "flake8==6.0.0\n"
    new_req = "flake8==6.1.0\n"
    delta = compute_dependency_delta({"requirements-dev.txt": (old_req, new_req)})
    assert not delta.is_empty
    assert len(delta.changed) == 1
    assert delta.changed[0].name == "flake8"


def test_go_mod_single_and_block_ignores_indirect():
    old_gomod = """module example.com/myapp

go 1.21

require github.com/gin-gonic/gin v1.9.0

require (
    github.com/google/uuid v1.3.0
    github.com/stretchr/testify v1.8.2 // indirect
    golang.org/x/crypto v0.9.0
)
"""
    new_gomod = """module example.com/myapp

go 1.21

require github.com/gin-gonic/gin v1.9.1

require (
    github.com/google/uuid v1.3.0
    github.com/stretchr/testify v1.8.4 // indirect
    github.com/gorilla/mux v1.8.0
    golang.org/x/text v0.9.0 // indirect
)
"""
    delta = compute_dependency_delta({"go.mod": (old_gomod, new_gomod)})

    assert not delta.is_empty
    assert delta.unparsed == []

    # Indirect dependencies must be completely ignored
    all_names = [c.name for c in delta.added + delta.removed + delta.changed]
    assert "github.com/stretchr/testify" not in all_names
    assert "golang.org/x/text" not in all_names

    # Changed: gin v1.9.0 -> v1.9.1
    changed_map = {c.name: c for c in delta.changed}
    assert "github.com/gin-gonic/gin" in changed_map
    assert changed_map["github.com/gin-gonic/gin"].old_version == "v1.9.0"
    assert changed_map["github.com/gin-gonic/gin"].new_version == "v1.9.1"
    assert changed_map["github.com/gin-gonic/gin"].ecosystem == "go"
    assert changed_map["github.com/gin-gonic/gin"].section == "require"

    # Added: gorilla/mux
    added_map = {c.name: c for c in delta.added}
    assert "github.com/gorilla/mux" in added_map
    assert added_map["github.com/gorilla/mux"].new_version == "v1.8.0"

    # Removed: golang.org/x/crypto
    removed_map = {c.name: c for c in delta.removed}
    assert "golang.org/x/crypto" in removed_map


def test_cargo_toml_dependencies_tables_and_targets():
    old_cargo = """[package]
name = "mycrate"
version = "0.1.0"

[dependencies]
serde = "1.0.180"
tokio = { version = "1.28.0", features = ["full"] }
old_crate = "0.2.0"

[dev-dependencies]
criterion = "0.4.0"

[build-dependencies]
cc = "1.0.70"

[workspace.dependencies]
shared-lib = { path = "../shared" }

[target.'cfg(unix)'.dependencies]
nix = "0.26.0"
"""
    new_cargo = """[package]
name = "mycrate"
version = "0.1.0"

[dependencies]
serde = "1.0.190"
tokio = { version = "1.30.0", features = ["full"] }
new_crate = { git = "https://github.com/example/new_crate", rev = "v1.0" }

[dev-dependencies]
criterion = "0.5.0"

[build-dependencies]
cc = "1.0.70"

[workspace.dependencies]
shared-lib = { path = "../shared-v2" }

[target.'cfg(unix)'.dependencies]
nix = "0.27.0"
"""
    delta = compute_dependency_delta({"Cargo.toml": (old_cargo, new_cargo)})

    assert not delta.is_empty
    assert delta.unparsed == []

    # Changed
    changed_map = {c.name: c for c in delta.changed}
    assert "serde" in changed_map
    assert changed_map["serde"].old_version == "1.0.180"
    assert changed_map["serde"].new_version == "1.0.190"
    assert "tokio" in changed_map
    assert changed_map["tokio"].old_version == "1.28.0"
    assert changed_map["tokio"].new_version == "1.30.0"
    assert "criterion" in changed_map
    assert "shared-lib" in changed_map
    assert "nix" in changed_map

    # Removed
    removed_map = {c.name: c for c in delta.removed}
    assert "old_crate" in removed_map

    # Added
    added_map = {c.name: c for c in delta.added}
    assert "new_crate" in added_map
    assert added_map["new_crate"].ecosystem == "rust"


def test_pom_xml_namespace_agnostic():
    old_pom = """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0"
         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 https://maven.apache.org/xsd/maven-4.0.0.xsd">
    <modelVersion>4.0.0</modelVersion>
    <dependencies>
        <dependency>
            <groupId>org.springframework.boot</groupId>
            <artifactId>spring-boot-starter-web</artifactId>
            <version>3.0.0</version>
        </dependency>
        <dependency>
            <groupId>commons-io</groupId>
            <artifactId>commons-io</artifactId>
            <version>2.11.0</version>
        </dependency>
    </dependencies>
</project>
"""
    new_pom = """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
    <modelVersion>4.0.0</modelVersion>
    <dependencies>
        <dependency>
            <groupId>org.springframework.boot</groupId>
            <artifactId>spring-boot-starter-web</artifactId>
            <version>3.1.0</version>
        </dependency>
        <dependency>
            <groupId>org.projectlombok</groupId>
            <artifactId>lombok</artifactId>
            <version>1.18.28</version>
        </dependency>
    </dependencies>
</project>
"""
    delta = compute_dependency_delta({"pom.xml": (old_pom, new_pom)})

    assert not delta.is_empty
    assert delta.unparsed == []

    changed_map = {c.name: c for c in delta.changed}
    assert "org.springframework.boot:spring-boot-starter-web" in changed_map
    assert changed_map["org.springframework.boot:spring-boot-starter-web"].old_version == "3.0.0"
    assert changed_map["org.springframework.boot:spring-boot-starter-web"].new_version == "3.1.0"
    assert changed_map["org.springframework.boot:spring-boot-starter-web"].ecosystem == "maven"

    removed_map = {c.name: c for c in delta.removed}
    assert "commons-io:commons-io" in removed_map

    added_map = {c.name: c for c in delta.added}
    assert "org.projectlombok:lombok" in added_map
    assert added_map["org.projectlombok:lombok"].new_version == "1.18.28"


def test_build_gradle_and_gradle_kts():
    old_gradle = """
plugins {
    id 'java'
}
dependencies {
    implementation 'org.springframework.boot:spring-boot-starter-web:3.0.0'
    testImplementation "org.junit.jupiter:junit-jupiter:5.9.0"
    api 'com.google.guava:guava:31.0-jre'
}
"""
    new_gradle = """
plugins {
    id 'java'
}
dependencies {
    implementation 'org.springframework.boot:spring-boot-starter-web:3.1.0'
    testImplementation "org.junit.jupiter:junit-jupiter:5.9.0"
    compileOnly 'org.projectlombok:lombok:1.18.28'
}
"""
    delta = compute_dependency_delta({"build.gradle": (old_gradle, new_gradle)})
    assert not delta.is_empty
    assert delta.unparsed == []

    changed_map = {c.name: c for c in delta.changed}
    assert "org.springframework.boot:spring-boot-starter-web" in changed_map
    assert changed_map["org.springframework.boot:spring-boot-starter-web"].old_version == "3.0.0"
    assert changed_map["org.springframework.boot:spring-boot-starter-web"].new_version == "3.1.0"
    assert changed_map["org.springframework.boot:spring-boot-starter-web"].ecosystem == "gradle"

    removed_map = {c.name: c for c in delta.removed}
    assert "com.google.guava:guava" in removed_map

    added_map = {c.name: c for c in delta.added}
    assert "org.projectlombok:lombok" in added_map
    assert added_map["org.projectlombok:lombok"].section == "compileOnly"


def test_gradle_kts_syntax():
    old_kts = 'dependencies {\n    implementation("com.example:lib:1.0.0")\n}\n'
    new_kts = 'dependencies {\n    implementation("com.example:lib:2.0.0")\n}\n'
    delta = compute_dependency_delta({"build.gradle.kts": (old_kts, new_kts)})
    assert len(delta.changed) == 1
    assert delta.changed[0].name == "com.example:lib"
    assert delta.changed[0].old_version == "1.0.0"
    assert delta.changed[0].new_version == "2.0.0"


def test_new_file_all_dependencies_added():
    pkg = '{"dependencies": {"react": "18.2.0", "next": "13.4.0"}}'
    delta = compute_dependency_delta({"package.json": (None, pkg)})
    assert not delta.is_empty
    assert len(delta.added) == 2
    assert len(delta.removed) == 0
    assert len(delta.changed) == 0
    for c in delta.added:
        assert c.old_version is None
        assert c.new_version is not None


def test_deleted_file_all_dependencies_removed():
    pkg = '{"dependencies": {"react": "18.2.0"}}'
    delta = compute_dependency_delta({"package.json": (pkg, None)})
    assert not delta.is_empty
    assert len(delta.added) == 0
    assert len(delta.removed) == 1
    assert len(delta.changed) == 0
    assert delta.removed[0].name == "react"
    assert delta.removed[0].old_version == "18.2.0"
    assert delta.removed[0].new_version is None


def test_malformed_input_goes_to_unparsed_no_exception():
    malformed_json = '{"dependencies": { INVALID JSON }}'
    delta = compute_dependency_delta({"package.json": ('{"dependencies": {}}', malformed_json)})
    assert not delta.is_empty
    assert delta.unparsed == ["package.json"]
    assert delta.added == []
    assert delta.removed == []
    assert delta.changed == []

    malformed_xml = '<project><dependencies><dependency><groupId>'
    delta_xml = compute_dependency_delta({"pom.xml": (malformed_xml, None)})
    assert delta_xml.unparsed == ["pom.xml"]
    assert delta_xml.added == []
    assert delta_xml.removed == []
    assert delta_xml.changed == []


def test_unsupported_manifest_goes_to_unparsed():
    delta = compute_dependency_delta({
        "setup.py": ("import setuptools", "import setuptools\n# edit"),
        "setup.cfg": ("[metadata]\nname = foo", None),
        "Pipfile": (None, "[[source]]\nurl = 'https://pypi.org/simple'"),
    })
    assert not delta.is_empty
    assert delta.unparsed == ["Pipfile", "setup.cfg", "setup.py"]
    assert delta.added == []
    assert delta.removed == []
    assert delta.changed == []


def test_lockfile_only_change_is_ignored():
    delta = compute_dependency_delta({
        "package-lock.json": ("{}", '{"version": 1}'),
        "yarn.lock": ("# lockfile", "# lockfile v2"),
        "pnpm-lock.yaml": ("lockfileVersion: 5.4", "lockfileVersion: 6.0"),
        "bun.lockb": ("bin", "bin2"),
        "bun.lock": ("{}", "{}"),
        "poetry.lock": ("[[package]]", "[[package]]"),
        "uv.lock": ("version = 1", "version = 1"),
        "Cargo.lock": ("[[package]]", "[[package]]"),
        "go.sum": ("h1:abc", "h1:def"),
        "Pipfile.lock": ("{}", "{}"),
    })
    assert delta.is_empty
    assert delta.unparsed == []
    assert delta.added == []
    assert delta.removed == []
    assert delta.changed == []


def test_non_manifest_paths_ignored():
    delta = compute_dependency_delta({
        "src/main.py": ("print(1)", "print(2)"),
        "README.md": (None, "# Title"),
        "tsconfig.json": ("{}", "{}"),
    })
    assert delta.is_empty
    assert delta.unparsed == []


def test_monorepo_subfolder_manifest():
    old_pkg = '{"dependencies": {"express": "4.17.1"}}'
    new_pkg = '{"dependencies": {"express": "4.18.2"}}'
    delta = compute_dependency_delta({"apps/api/package.json": (old_pkg, new_pkg)})
    assert not delta.is_empty
    assert delta.unparsed == []
    assert len(delta.changed) == 1
    assert delta.changed[0].manifest == "apps/api/package.json"
    assert delta.changed[0].name == "express"


def test_deterministic_byte_identical_json():
    changes = {
        "package.json": (
            '{"dependencies": {"react": "17.0.0", "alpha": "1.0.0"}}',
            '{"dependencies": {"react": "18.0.0", "beta": "2.0.0"}}',
        ),
        "pom.xml": (
            '<project><dependencies><dependency><groupId>g</groupId><artifactId>b</artifactId><version>1</version></dependency></dependencies></project>',
            '<project><dependencies><dependency><groupId>g</groupId><artifactId>a</artifactId><version>2</version></dependency></dependencies></project>',
        ),
    }
    delta1 = compute_dependency_delta(changes)
    delta2 = compute_dependency_delta(changes)
    json1 = delta1.model_dump_json()
    json2 = delta2.model_dump_json()
    assert json1 == json2


def test_to_summary_text():
    changes = {
        "package.json": (
            '{"dependencies": {"react": "17.0.0"}}',
            '{"dependencies": {"react": "18.0.0", "axios": "1.0.0"}}',
        ),
        "pom.xml": ("invalid xml", "<project></project>"),
    }
    delta = compute_dependency_delta(changes)
    summary = delta.to_summary_text()
    assert "added: axios 1.0.0" in summary or "added: axios (1.0.0)" in summary or "axios" in summary
    assert "changed:" in summary and "react" in summary
    assert "unparsed: pom.xml" in summary


def test_no_io_guarantee():
    changes = {
        "package.json": (
            '{"dependencies": {"react": "17.0.0"}}',
            '{"dependencies": {"react": "18.0.0"}}',
        ),
    }
    def _raise_io(*args, **kwargs):
        raise RuntimeError("I/O is forbidden in compute_dependency_delta")

    with patch("builtins.open", side_effect=_raise_io), \
         patch("subprocess.run", side_effect=_raise_io), \
         patch("subprocess.Popen", side_effect=_raise_io):
        delta = compute_dependency_delta(changes)
        assert not delta.is_empty
        assert len(delta.changed) == 1


def test_requirements_pip_compile_line_continuations_and_hashes():
    old_req = """# Generated by pip-compile
flask==2.2.0 \\
    --hash=sha256:1111111111111111111111111111111111111111111111111111111111111111 \\
    --hash=sha256:2222222222222222222222222222222222222222222222222222222222222222
requests==2.28.0 \\
    --hash=sha256:3333333333333333333333333333333333333333333333333333333333333333
"""
    # Unchanged file with line continuations and hashes -> empty delta
    unchanged_delta = compute_dependency_delta({"requirements.txt": (old_req, old_req)})
    assert unchanged_delta.is_empty

    # Real bump with new hashes -> clean old/new version
    new_req = """# Generated by pip-compile
flask==2.3.0 \\
    --hash=sha256:4444444444444444444444444444444444444444444444444444444444444444
requests==2.28.0 \\
    --hash=sha256:3333333333333333333333333333333333333333333333333333333333333333
"""
    delta = compute_dependency_delta({"requirements.txt": (old_req, new_req)})
    assert not delta.is_empty
    assert len(delta.changed) == 1
    ch = delta.changed[0]
    assert ch.name == "flask"
    assert ch.old_version == "==2.2.0"
    assert ch.new_version == "==2.3.0"
    assert len(delta.added) == 0
    assert len(delta.removed) == 0


def test_pep503_normalization_pyproject_and_requirements():
    # In requirements.txt: Flask_SQLAlchemy vs Flask.SQLAlchemy vs flask-sqlalchemy
    req1 = "Flask_SQLAlchemy == 3.0.0\n"
    req2 = "Flask.SQLAlchemy == 3.0.0\n"
    req3 = "flask-sqlalchemy == 3.0.0\n"
    delta1 = compute_dependency_delta({"requirements.txt": (req1, req2)})
    assert delta1.is_empty
    delta2 = compute_dependency_delta({"requirements.txt": (req2, req3)})
    assert delta2.is_empty

    # In pyproject.toml:
    toml1 = '[project]\ndependencies = ["Flask_SQLAlchemy>=3.0.0"]\n'
    toml2 = '[project]\ndependencies = ["Flask.SQLAlchemy>=3.0.0"]\n'
    toml3 = '[project]\ndependencies = ["flask-sqlalchemy>=3.0.0"]\n'
    delta_t1 = compute_dependency_delta({"pyproject.toml": (toml1, toml2)})
    assert delta_t1.is_empty
    delta_t2 = compute_dependency_delta({"pyproject.toml": (toml2, toml3)})
    assert delta_t2.is_empty


def test_pom_xml_distinct_section_labels():
    pom_content = """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
    <modelVersion>4.0.0</modelVersion>
    <dependencies>
        <dependency>
            <groupId>org.example</groupId>
            <artifactId>proj-dep</artifactId>
            <version>1.0.0</version>
        </dependency>
    </dependencies>
    <dependencyManagement>
        <dependencies>
            <dependency>
                <groupId>org.example</groupId>
                <artifactId>mgmt-dep</artifactId>
                <version>2.0.0</version>
            </dependency>
        </dependencies>
    </dependencyManagement>
    <build>
        <plugins>
            <plugin>
                <groupId>org.example</groupId>
                <artifactId>my-plugin</artifactId>
                <version>1.0</version>
                <dependencies>
                    <dependency>
                        <groupId>org.example</groupId>
                        <artifactId>plugin-dep</artifactId>
                        <version>3.0.0</version>
                    </dependency>
                </dependencies>
            </plugin>
        </plugins>
    </build>
</project>
"""
    delta = compute_dependency_delta({"pom.xml": (None, pom_content)})
    assert not delta.is_empty
    sections_by_name = {c.name: c.section for c in delta.added}
    assert sections_by_name["org.example:proj-dep"] == "dependencies"
    assert sections_by_name["org.example:mgmt-dep"] == "dependencyManagement"
    assert sections_by_name["org.example:plugin-dep"] == "plugin-dependencies"


def test_unsupported_manifests_extended_and_lockfiles():
    changes = {
        "Gemfile": ("source 'https://rubygems.org'", None),
        "composer.json": ('{"require": {}}', None),
        "build.sbt": ('name := "test"', None),
        "pubspec.yaml": ('name: flutter_app', None),
        "mix.exs": ('defp deps do [] end', None),
        "Package.swift": ('// swift-tools-version:5.5', None),
        "Podfile": ("platform :ios, '14.0'", None),
        "go.work": ("go 1.21", None),
        "libs.versions.toml": ("[versions]", None),
        "deno.json": ("{}", None),
        "deno.jsonc": ("{}", None),
        "environment.yml": ("name: env", None),
        "environment.yaml": ("name: env", None),
        "Directory.Packages.props": ("<Project></Project>", None),
        "packages.config": ("<packages></packages>", None),
        "MyApp.csproj": ("<Project></Project>", None),
        "MyLib.fsproj": ("<Project></Project>", None),
        "Legacy.vbproj": ("<Project></Project>", None),
        "custom.gemspec": ("Gem::Specification.new do |s| end", None),
        "MyPod.podspec": ("Pod::Spec.new do |s| end", None),
        "package-lock.json": ("{}", "{}"),
    }
    delta = compute_dependency_delta(changes)
    assert not delta.is_empty
    # package-lock.json is ignored (not unparsed)
    assert "package-lock.json" not in delta.unparsed
    # All 20 unsupported manifests must be in unparsed
    expected_unparsed = sorted([
        "Gemfile",
        "composer.json",
        "build.sbt",
        "pubspec.yaml",
        "mix.exs",
        "Package.swift",
        "Podfile",
        "go.work",
        "libs.versions.toml",
        "deno.json",
        "deno.jsonc",
        "environment.yml",
        "environment.yaml",
        "Directory.Packages.props",
        "packages.config",
        "MyApp.csproj",
        "MyLib.fsproj",
        "Legacy.vbproj",
        "custom.gemspec",
        "MyPod.podspec",
    ])
    assert delta.unparsed == expected_unparsed


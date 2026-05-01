#!/usr/bin/env python3
"""
Build script: gera o ZIP do plugin e atualiza docs/plugins.xml.

Uso local:
    python scripts/build_plugin.py          # usa versão do metadata.txt
    python scripts/build_plugin.py 1.0.1    # sobrescreve versão

O ZIP gerado vai para docs/releases/agroexport.<version>.zip
"""
import os
import re
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT        = Path(__file__).resolve().parent.parent
PLUGIN_DIR  = ROOT / "agroexport"
METADATA    = PLUGIN_DIR / "metadata.txt"
DOCS_DIR    = ROOT / "docs"
RELEASES    = DOCS_DIR / "releases"
PLUGINS_XML = DOCS_DIR / "plugins.xml"
BASE_URL    = "https://lc4pr1o.github.io/expogeo"
REPO_URL    = "https://github.com/Lc4pr1o/expogeo"

SKIP = {"__pycache__", ".DS_Store", "Thumbs.db"}
SKIP_EXT = {".pyc", ".pyo"}


import configparser as _cp

def _meta_parser() -> _cp.ConfigParser:
    p = _cp.ConfigParser(strict=False)
    p.read_string("[general]\n" + METADATA.read_text(encoding="utf-8"))
    return p

def read_meta(key: str) -> str:
    try:
        return _meta_parser().get("general", key).strip()
    except (_cp.NoSectionError, _cp.NoOptionError):
        return ""


def set_meta_version(version: str) -> None:
    text = METADATA.read_text(encoding="utf-8")
    text = re.sub(r"^version=.*", f"version={version}", text, flags=re.MULTILINE)
    METADATA.write_text(text, encoding="utf-8")
    print(f"  metadata.txt -> version={version}")


def build_zip(version: str) -> Path:
    RELEASES.mkdir(parents=True, exist_ok=True)
    zip_path = RELEASES / f"agroexport.{version}.zip"

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(PLUGIN_DIR.rglob("*")):
            if not f.is_file():
                continue
            if any(part in SKIP for part in f.parts):
                continue
            if f.suffix in SKIP_EXT:
                continue
            arcname = "agroexport/" + f.relative_to(PLUGIN_DIR).as_posix()
            zf.write(f, arcname)

    print(f"  ZIP -> {zip_path.relative_to(ROOT)}")
    return zip_path


def update_plugins_xml(version: str, zip_path: Path) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000Z")
    desc    = read_meta("description")
    about   = read_meta("about")
    author  = read_meta("author")
    qmin    = read_meta("qgisMinimumVersion")
    qmax    = read_meta("qgisMaximumVersion")
    tags    = read_meta("tags")
    tracker = read_meta("tracker") or f"{REPO_URL}/issues"

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<plugins>
  <pyqgis_plugin name="AgroExport" version="{version}">
    <description><![CDATA[{desc}]]></description>
    <about><![CDATA[{about}]]></about>
    <version>{version}</version>
    <trusted>False</trusted>
    <qgis_minimum_version>{qmin}</qgis_minimum_version>
    <qgis_maximum_version>{qmax}</qgis_maximum_version>
    <homepage>{REPO_URL}</homepage>
    <file_name>{zip_path.name}</file_name>
    <icon/>
    <author_name>{author}</author_name>
    <download_url>{BASE_URL}/releases/{zip_path.name}</download_url>
    <uploaded_by>Lc4pr1o</uploaded_by>
    <create_date>{now}</create_date>
    <update_date>{now}</update_date>
    <experimental>False</experimental>
    <deprecated>False</deprecated>
    <server>False</server>
    <external_deps/>
    <tracker>{tracker}</tracker>
    <repository>{REPO_URL}</repository>
    <tags>{tags}</tags>
    <downloads>0</downloads>
    <average_vote>0</average_vote>
    <rating_votes>0</rating_votes>
    <changelog><![CDATA[{read_meta("changelog")}]]></changelog>
  </pyqgis_plugin>
</plugins>
"""
    PLUGINS_XML.write_text(xml, encoding="utf-8")
    print(f"  plugins.xml -> version={version}")


def update_index_html(version: str) -> None:
    html = (DOCS_DIR / "index.html").read_text(encoding="utf-8")
    html = re.sub(r'<span class="badge">v[\d.]+</span>',
                  f'<span class="badge">v{version}</span>', html)
    (DOCS_DIR / "index.html").write_text(html, encoding="utf-8")
    print(f"  index.html  -> version={version}")


if __name__ == "__main__":
    version = sys.argv[1] if len(sys.argv) > 1 else read_meta("version")
    if not version:
        sys.exit("Erro: não foi possível determinar a versão.")

    print(f"\nBuild AgroExport v{version}\n")
    set_meta_version(version)
    zip_path = build_zip(version)
    update_plugins_xml(version, zip_path)
    update_index_html(version)
    print(f"\nConcluido. Faca commit de docs/ e agroexport/metadata.txt")
    print(f"   -> QGIS URL: {BASE_URL}/plugins.xml\n")

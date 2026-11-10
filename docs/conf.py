# Configuration file for the Sphinx documentation builder.
#
# This file only contains a selection of the most common options. For a full
# list see the documentation:
# https://www.sphinx-doc.org/en/master/usage/configuration.html

import re

# Hardwire version number instead of extracting from the repo.
version_number = "1.16.0"

# Minimum supported ROCm version, substituted into docs as {__ROCM_MIN_VERSION__}.
rocm_min_version = "6.3.0"

left_nav_title = f"ROCm Omnistat 1.16.0 Documentation"

exclude_patterns = [
    'conceptual/**',
    'how to/**',
    'schemas/**',
]

# for PDF output on Read the Docs
project = "ROCm Omnistat Documentation"
author = "Advanced Micro Devices, Inc."
copyright = "Copyright (c) 2023-2026 Advanced Micro Devices, Inc. All rights reserved."
version = version_number
release = version_number

html_title = left_nav_title
html_static_path = ["static"]
extensions = ["rocm_docs"]
myst_heading_anchors = 4
html_theme = "rocm_docs_theme"
html_theme_options = {
    "flavor": "rocm-extras",
    "header_title": f"ROCm™ Omnistat 1.16.0",
    "header_link": f"https://rocm.docs.amd.com/projects/omnistat/en/docs-1.16.0/",
    "version_list_link": f"https://rocm.docs.amd.com/projects/omnistat/en/docs-1.16.0/versions.html",
    "link_main_doc": True,
}

html_theme_options.update(
    {
        "repository_url": "https://github.com/ROCm/omnistat",
        "path_to_docs": "docs",
        "use_repository_button": True,
        "use_issues_button": True,
        "use_download_button": True,
    }
)

external_projects_current_project = "rocmvalidationsuite"

# Temporary setting for local build. REMOVE before checking in
external_projects_remote_repository=""

external_toc_path = "./sphinx/_toc.yml"
external_projects = []

# ---------------------------------------------------------------------------
# Public wheel availability toggle.
# ---------------------------------------------------------------------------
release_wheels_public = True

_CONDITIONAL_BLOCK = re.compile(
    r"^[ \t]*<!--[ \t]*if-(no)?release:start[ \t]*-->[ \t]*\n"
    r"(.*?)"
    r"^[ \t]*<!--[ \t]*if-(?:no)?release:end[ \t]*-->[ \t]*\n",
    re.DOTALL | re.MULTILINE,
)


def toggleReleaseContent(app, docname, source):
    public = app.config.release_wheels_public

    def keepOrDrop(match):
        negated = match.group(1) is not None
        return match.group(2) if (public != negated) else ""

    source[0] = _CONDITIONAL_BLOCK.sub(keepOrDrop, source[0])


def replaceString(app, docname, source):
    result = source[0]
    for key in app.config.docstring_replacements:
        result = result.replace(key, app.config.docstring_replacements[key])
    source[0] = result


def setup(app):
    app.add_config_value("docstring_replacements", {}, True)
    app.add_config_value("release_wheels_public", False, "env")
    app.connect("source-read", replaceString)
    app.connect("source-read", toggleReleaseContent)


docstring_replacements = {
    "{__VERSION__}": version_number,
    "{__ROCM_MIN_VERSION__}": rocm_min_version,
}
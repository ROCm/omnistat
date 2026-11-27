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

left_nav_title = f"ROCm Omnistat {version_number} Documentation"

# for PDF output on Read the Docs
project = "ROCm Omnistat Documentation"
author = "Advanced Micro Devices, Inc."
copyright = "Copyright (c) 2023-2026 Advanced Micro Devices, Inc. All rights reserved."
version = version_number
release = version_number

html_title = left_nav_title
html_static_path = ["static"]
# Named omnistat.css rather than custom.css on purpose: html_static_path is
# copied over the theme's own static files, and rocm_docs_theme ships a
# static/custom.css of its own, which a same-named file here silently replaces.
html_css_files = ["omnistat.css"]
extensions = ["rocm_docs"]
myst_heading_anchors = 4
numfig = True
numfig_format = {"figure": "Figure %s"}

html_theme = "rocm_docs_theme"
html_theme_options = {
    "flavor": "rocm-extras",
    "header_title": f"ROCm™ Omnistat {version_number}",
    "header_link": "https://rocm.docs.amd.com/projects/omnistat/en/latest/",
    "version_list_link": "https://rocm.docs.amd.com/projects/omnistat/en/latest/versions.html",
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

external_projects_current_project = "omnistat"
external_projects_remote_repository = ""
external_projects = []

external_toc_path = "./sphinx/_toc.yml"

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

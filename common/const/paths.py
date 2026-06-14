#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Path constants and root-directory helpers."""
import os
from pathlib import Path

from loguru import logger

import metagpt


def get_metagpt_package_root():
    """Get the root directory of the installed package."""
    package_root = Path(metagpt.__file__).parent.parent
    logger.info(f"Package root set to {str(package_root)}")
    return package_root


def get_metagpt_root():
    """Get the project root directory."""
    # Check if a project root is specified in the environment variable
    project_root_env = os.getenv("METAGPT_PROJECT_ROOT")
    if project_root_env:
        project_root = Path(project_root_env)
        logger.info(f"PROJECT_ROOT set from environment variable to {str(project_root)}")
    else:
        # Fallback to package root if no environment variable is set
        project_root = get_metagpt_package_root()
        for i in (".git", ".project_root", ".gitignore"):
            if (project_root / i).exists():
                break
        else:
            project_root = Path.cwd()

    return project_root


# METAGPT PROJECT ROOT AND VARS
CONFIG_ROOT = Path.home() / ".metagpt"
METAGPT_ROOT = get_metagpt_root()  # Dependent on METAGPT_PROJECT_ROOT
DEFAULT_WORKSPACE_ROOT = METAGPT_ROOT / "workspace"


def get_backend_readme_path() -> Path:
    return DEFAULT_WORKSPACE_ROOT / "app" / "backend" / "README.md"


def get_frontend_readme_path() -> Path:
    return DEFAULT_WORKSPACE_ROOT / "app" / "frontend" / "README.md"


def set_default_workspace_root(new_path: Path):
    global DEFAULT_WORKSPACE_ROOT
    logger.warning(f"update DEFAULT_WORKSPACE_ROOT from: {DEFAULT_WORKSPACE_ROOT} to: {new_path}")
    DEFAULT_WORKSPACE_ROOT = new_path


# Deprecated: these are frozen at import time and won't reflect set_default_workspace_root().
# Use get_backend_readme_path() / get_frontend_readme_path() instead.
BACKEND_README_PATH = DEFAULT_WORKSPACE_ROOT / "app" / "backend" / "README.md"
FRONTEND_README_PATH = DEFAULT_WORKSPACE_ROOT / "app" / "frontend" / "README.md"

EXAMPLE_PATH = METAGPT_ROOT / "examples"
EXAMPLE_DATA_PATH = EXAMPLE_PATH / "data"
DATA_PATH = METAGPT_ROOT / "data"
TEST_DATA_PATH = METAGPT_ROOT / "tests/data"
RESEARCH_PATH = DATA_PATH / "research"

UT_PATH = DATA_PATH / "ut"
SWAGGER_PATH = UT_PATH / "files/api/"
UT_PY_PATH = UT_PATH / "files/ut/"
API_QUESTIONS_PATH = UT_PATH / "files/question/"

ATOMS_DIR_NAME = ".atoms"

# P1 Context Protocol file names
CONTEXT_FILE_ATOMS = "ATOMS.md"
CONTEXT_FILE_PROGRESS = "PROGRESS.md"
CONTEXT_FILE_ARCHITECTURE = "ARCHITECTURE.md"
CONTEXT_REPORTS_DIR = "reports"

SERDESER_PATH = DEFAULT_WORKSPACE_ROOT / "storage"  # TODO to store `storage` under the individual generated project

TMP = METAGPT_ROOT / "tmp"

SOURCE_ROOT = METAGPT_ROOT / "metagpt"
PROMPT_PATH = SOURCE_ROOT / "prompts"
SKILL_DIRECTORY = SOURCE_ROOT / "skills"
TOOL_SCHEMA_PATH = METAGPT_ROOT / "metagpt/tools/schemas"
TOOL_LIBS_PATH = METAGPT_ROOT / "metagpt/tools/libs"

# TEMPLATE PATH
TEMPLATE_FOLDER_PATH = METAGPT_ROOT / "mgx_template" / "templates"
DEFAULT_WEB_TEMPLATE_FOLDER_PATH = TEMPLATE_FOLDER_PATH / "default_web_project"
VUE_TEMPLATE_PATH = DEFAULT_WEB_TEMPLATE_FOLDER_PATH / "vue_template"
REACT_TEMPLATE_PATH = DEFAULT_WEB_TEMPLATE_FOLDER_PATH / "react_template"
SLIDE_TEMPLATE_PATH = TEMPLATE_FOLDER_PATH / "presentation"
PROTOTYPE_TEMPLATE_PATH = TEMPLATE_FOLDER_PATH / "prototype_template"

# FuncSea templates (React + shadcn/ui + Tailwind CSS)
FUNCSEA_TEMPLATE_ROOT = TEMPLATE_FOLDER_PATH / "function_sea"
FRONTEND_TEMPLATE_PATH = FUNCSEA_TEMPLATE_ROOT / "templates" / "base" / "frontend"
SHADCN_UI_TEMPLATE_PATH = FRONTEND_TEMPLATE_PATH  # Backward compatibility alias

DOCS_FILE_REPO = "docs"
PRDS_FILE_REPO = "docs/prd"
SYSTEM_DESIGN_FILE_REPO = "docs/system_design"
TASK_FILE_REPO = "docs/task"
CODE_PLAN_AND_CHANGE_FILE_REPO = "docs/code_plan_and_change"
COMPETITIVE_ANALYSIS_FILE_REPO = "resources/competitive_analysis"
DATA_API_DESIGN_FILE_REPO = "resources/data_api_design"
SEQ_FLOW_FILE_REPO = "resources/seq_flow"
SYSTEM_DESIGN_PDF_FILE_REPO = "resources/system_design"
PRD_PDF_FILE_REPO = "resources/prd"
TASK_PDF_FILE_REPO = "resources/api_spec_and_task"
CODE_PLAN_AND_CHANGE_PDF_FILE_REPO = "resources/code_plan_and_change"
TEST_CODES_FILE_REPO = "tests"
TEST_OUTPUTS_FILE_REPO = "test_outputs"
CODE_SUMMARIES_FILE_REPO = "docs/code_summary"
CODE_SUMMARIES_PDF_FILE_REPO = "resources/code_summary"
RESOURCES_FILE_REPO = "resources"
SD_OUTPUT_FILE_REPO = DEFAULT_WORKSPACE_ROOT
GRAPH_REPO_FILE_REPO = "docs/graph_repo"
VISUAL_GRAPH_REPO_FILE_REPO = "resources/graph_db"
CLASS_VIEW_FILE_REPO = "docs/class_view"

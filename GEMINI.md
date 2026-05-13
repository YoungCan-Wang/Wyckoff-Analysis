# Project Architecture & Workflow Conventions

## 1. Frontend Architecture
*   **React (`web/`) is the production web surface.** New user-facing capabilities should be implemented in the Agent/chat experience rather than new standalone routes.
*   **Streamlit (`app/` and `streamlit_app.py`) is maintenance-only.** It can keep historical export or diagnostic utilities, but it is not the grey-release path for new product features.
*   **Agent Rule:** Do not suggest deleting `app/` just because it is maintenance code, but do not route new feature work through Streamlit first.

## 2. Documentation Structure
*   **Wiki Visibility:** The `wiki_repo_new/` directory is **intentionally kept hidden** (ignored via `.gitignore`).
*   **Agent Rule:** Do NOT suggest removing `wiki_repo_new/` from `.gitignore` or complain about its invisibility in the repository. Do not suggest merging its contents into `docs/` unless explicitly requested by the user.

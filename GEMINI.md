# Project Architecture & Workflow Conventions

## 1. Frontend Architecture & Grey-Release Strategy
*   **Streamlit (`app/`) is NOT legacy code to be deleted.** It serves as the project's **grey-release (canary) testing environment**.
*   **Workflow:** New capabilities, logic updates, and features must first be verified in the Streamlit app. Only after their functionality is proven correct and stable should they be migrated to the production React application (`web/`) on Cloudflare Pages.
*   **Agent Rule:** Do NOT suggest deleting the `app/` directory or flagging it as redundant code.

## 2. Documentation Structure
*   **Wiki Visibility:** The `wiki_repo_new/` directory is **intentionally kept hidden** (ignored via `.gitignore`).
*   **Agent Rule:** Do NOT suggest removing `wiki_repo_new/` from `.gitignore` or complain about its invisibility in the repository. Do not suggest merging its contents into `docs/` unless explicitly requested by the user.
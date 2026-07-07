"""Body-rendering pipeline for HKLII judgment HTML.

- text.py: text-node walker (shared by FTS indexer + citation highlighter)
- sanitizer.py (Phase 3): lxml allowlist walker for render-time cleanup
- render.py (Phase 3): full render pipeline with cache

See docs/viewer-design.md §5.
"""

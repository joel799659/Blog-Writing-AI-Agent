# Frontend (`bwa_frontend.py`)


from __future__ import annotations

import json
import re
import zipfile
from datetime import date
from io import BytesIO
from pathlib import Path
from typing import Any, Dict

import pandas as pd
import streamlit as st

from bwa_backend import app


# ============================================================
# Page Config
# ============================================================

st.set_page_config(page_title="Blog Writing Agent", layout="wide")

st.markdown(
    """
<style>
.main .block-container {
    max-width: 900px;
    padding-top: 2rem;
}

p {
    line-height: 1.8;
    font-size: 1.05rem;
}

h1, h2, h3 {
    margin-top: 1.5rem;
}
</style>
""",
    unsafe_allow_html=True,
)


# ============================================================
# Session State
# ============================================================

if "logs" not in st.session_state:
    st.session_state["logs"] = []

if "last_out" not in st.session_state:
    st.session_state["last_out"] = None


# ============================================================
# Helpers
# ============================================================

BASE_DIR = Path(__file__).parent



def safe_slug(title: str):
    s = title.strip().lower()
    s = re.sub(r"[^a-z0-9 _-]+", "", s)
    s = re.sub(r"\s+", "_", s).strip("_")
    return s or "blog"



def log(msg: str):
    st.session_state["logs"].append(msg)



def try_stream(graph_app, inputs):
    final_state = None

    for step in graph_app.stream(inputs, stream_mode="updates"):
        final_state = step
        yield ("updates", step)

    yield ("final", final_state)



def render_markdown(md: str):
    import re
    from pathlib import Path

    image_pattern = r'!\[(.*?)\]\((.*?)\)'

    parts = re.split(image_pattern, md)

    i = 0

    while i < len(parts):

        text = parts[i]

        if text:
            st.markdown(text)

        if i + 2 < len(parts):

            alt = parts[i + 1]
            path = parts[i + 2]

            img_path = Path(path)

            if img_path.exists():
                st.image(str(img_path), caption=alt)
            else:
                st.warning(f"Image not found: {path}")

        i += 3



def bundle_zip(md_text, md_filename, images_dir: Path):
    buf = BytesIO()

    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr(md_filename, md_text.encode("utf-8"))

        if images_dir.exists():
            for p in images_dir.rglob("*"):
                if p.is_file():
                    z.write(p, arcname=str(p))

    return buf.getvalue()


# ============================================================
# UI
# ============================================================

st.title("Blog Writing Agent")

with st.sidebar:
    st.header("Generate New Blog")

    topic = st.text_area("Topic", height=120)

    as_of = st.date_input("As-of date", value=date.today())

    run_btn = st.button("🚀 Generate Blog", type="primary")

    st.divider()
    st.subheader("Past blogs")

    blog_files = sorted(
        Path(".").glob("*.md"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    if not blog_files:
        st.caption("No blogs found")

    else:
        selected_blog = st.radio(
            "Select blog",
            options=[p.name for p in blog_files[:30]],
            label_visibility="collapsed",
        )

        if st.button("📂 Load selected blog"):
            md = Path(selected_blog).read_text(encoding="utf-8")

            st.session_state["last_out"] = {
                "final": md,
                "plan": None,
                "evidence": [],
            }

            st.rerun()


# ============================================================
# Tabs
# ============================================================

plan_tab, evidence_tab, preview_tab, logs_tab = st.tabs(
    [
        "🧩 Plan",
        "🔎 Evidence",
        "📝 Preview",
        "🧾 Logs",
    ]
)


# ============================================================
# Live Execution Panels
# ============================================================

execution_container = st.container()
json_container = st.empty()


# ============================================================
# Run Graph
# ============================================================

if run_btn:
    if not topic.strip():
        st.warning("Please enter a topic")
        st.stop()

    inputs: Dict[str, Any] = {
        "topic": topic,
        "mode": "",
        "needs_research": False,
        "queries": [],
        "evidence": [],
        "plan": None,
        "as_of": as_of.isoformat(),
        "recency_days": 7,
        "sections": [],
        "merged_md": "",
        "md_with_placeholders": "",
        "image_specs": [],
        "blog_slug": "",
        "images_dir": "",
        "final": "",
    }

    status = st.status("Running graph...", expanded=True)

    latest_state = {}
    visited_nodes = []

    execution_placeholder = execution_container.empty()

    for kind, payload in try_stream(app, inputs):

        if not isinstance(payload, dict):
            continue

        # STREAM MODE RETURNS:
        # {"router": {...}}
        # {"research": {...}}

        if (
            len(payload) == 1
            and isinstance(next(iter(payload.values())), dict)
        ):
            node_name = next(iter(payload.keys()))
            node_data = next(iter(payload.values()))

            visited_nodes.append(node_name)

            # IMPORTANT:
            # MERGE NODE OUTPUT INTO GLOBAL STATE
            latest_state.update(node_data)

        else:
            latest_state.update(payload)

        visited_nodes = list(dict.fromkeys(visited_nodes))

        with execution_placeholder.container():

            st.markdown("## Running graph...")

            for node in visited_nodes:
                st.markdown(f"➡️ Node: `{node}`")

        summary = {
            "mode": latest_state.get("mode"),
            "needs_research": latest_state.get("needs_research"),
            "queries": latest_state.get("queries", []),
            "evidence_count": len(latest_state.get("evidence", []) or []),
            "sections_done": len(latest_state.get("sections", []) or []),
            "images": len(latest_state.get("image_specs", []) or []),
        }

        json_container.json(summary)

        log(json.dumps(summary, default=str))

# FINAL OUTPUT
    final_output = app.invoke(inputs)

    st.session_state["last_out"] = final_output

    status.update(label="✅ Completed", state="complete")


# ============================================================
# Render Output
# ============================================================

out = st.session_state.get("last_out")

if out:

    # --------------------------------------------------------
    # Plan
    # --------------------------------------------------------

    with plan_tab:
        st.subheader("Plan")

        plan = out.get("plan")

        if hasattr(plan, "model_dump"):
            plan = plan.model_dump()

        if plan:
            st.write("###", plan.get("blog_title"))

            tasks = plan.get("tasks", [])

            if tasks:
                df = pd.DataFrame(tasks)
                st.dataframe(df, use_container_width=True)


    # --------------------------------------------------------
    # Evidence
    # --------------------------------------------------------

    with evidence_tab:
        st.subheader("Evidence")

        evidence = out.get("evidence", [])

        if evidence:
            rows = []

            for e in evidence:
                if hasattr(e, "model_dump"):
                    e = e.model_dump()

                rows.append(e)

            st.dataframe(pd.DataFrame(rows), use_container_width=True)

        else:
            st.info("No evidence found")


    # --------------------------------------------------------
    # Preview
    # --------------------------------------------------------

    with preview_tab:
        final_md = out.get("final", "")

        if final_md:
            with st.container(border=True):
                render_markdown(final_md)

            plan = out.get("plan")

            if hasattr(plan, "blog_title"):
                title = plan.blog_title
            elif isinstance(plan, dict):
                title = plan.get("blog_title", "blog")
            else:
                title = "blog"

            md_filename = f"{safe_slug(title)}.md"

            st.download_button(
                "⬇ Download Markdown",
                data=final_md.encode("utf-8"),
                file_name=md_filename,
                mime="text/markdown",
            )

            images_dir = Path(out.get("images_dir", "images"))

            bundle = bundle_zip(final_md, md_filename, images_dir)

            st.download_button(
                "📦 Download Bundle",
                data=bundle,
                file_name=f"{safe_slug(title)}_bundle.zip",
                mime="application/zip",
            )


    # --------------------------------------------------------
    # Logs
    # --------------------------------------------------------

    with logs_tab:
        st.text_area(
            "Logs",
            value="\n".join(st.session_state["logs"][-100:]),
            height=500,
        )

else:
    st.info("Enter a topic and click Generate Blog")

# Corrected LangGraph Blog Writer — Backend + Frontend

## Backend (`bwa_backend.py`)


from __future__ import annotations

import operator
import os
import re
from datetime import date, timedelta
from pathlib import Path
from typing import TypedDict, List, Optional, Literal, Annotated

from pydantic import BaseModel, Field

from langgraph.graph import StateGraph, START, END
from langgraph.types import Send

from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage
from dotenv import load_dotenv

load_dotenv()

# ============================================================
# Schemas
# ============================================================

class Task(BaseModel):
    id: int
    title: str
    goal: str
    bullets: List[str] = Field(..., min_length=3, max_length=6)
    target_words: int

    tags: List[str] = Field(default_factory=list)
    requires_research: bool = False
    requires_citations: bool = False
    requires_code: bool = False


class Plan(BaseModel):
    blog_title: str
    audience: str
    tone: str
    blog_kind: Literal[
        "explainer",
        "tutorial",
        "news_roundup",
        "comparison",
        "system_design",
    ] = "explainer"

    constraints: List[str] = Field(default_factory=list)
    tasks: List[Task]


class EvidenceItem(BaseModel):
    title: str
    url: str
    published_at: Optional[str] = None
    snippet: Optional[str] = None
    source: Optional[str] = None


class RouterDecision(BaseModel):
    needs_research: bool
    mode: Literal["closed_book", "hybrid", "open_book"]
    reason: str
    queries: List[str] = Field(default_factory=list)
    max_results_per_query: int = 5


class EvidencePack(BaseModel):
    evidence: List[EvidenceItem] = Field(default_factory=list)


class ImageSpec(BaseModel):
    placeholder: str
    filename: str
    alt: str
    caption: str
    prompt: str
    size: Literal["1024x1024", "1024x1536", "1536x1024"] = "1024x1024"
    quality: Literal["low", "medium", "high"] = "medium"


class GlobalImagePlan(BaseModel):
    md_with_placeholders: str
    images: List[ImageSpec] = Field(default_factory=list)


class State(TypedDict):
    topic: str

    mode: str
    needs_research: bool
    queries: List[str]
    evidence: List[EvidenceItem]
    plan: Optional[Plan]

    as_of: str
    recency_days: int

    sections: Annotated[List[tuple[int, str]], operator.add]

    merged_md: str
    md_with_placeholders: str
    image_specs: List[dict]

    blog_slug: str
    images_dir: str

    final: str


# ============================================================
# LLM
# ============================================================

llm = ChatOpenAI(model="gpt-4.1-mini", temperature=0.3)


# ============================================================
# Router
# ============================================================

ROUTER_SYSTEM = """
You are a routing module for a technical blog planner.

Decide whether web research is needed BEFORE planning.

Modes:
- closed_book: evergreen concepts.
- hybrid: evergreen + latest examples/tools/models.
- open_book: news/current-events/weekly roundups.

If needs_research=true:
- Generate 3-10 focused web queries.
"""


def router_node(state: State) -> dict:
    decider = llm.with_structured_output(RouterDecision)

    decision = decider.invoke(
        [
            SystemMessage(content=ROUTER_SYSTEM),
            HumanMessage(
                content=f"Topic: {state['topic']}\nAs-of: {state['as_of']}"
            ),
        ]
    )

    if decision.mode == "open_book":
        recency_days = 7
    elif decision.mode == "hybrid":
        recency_days = 45
    else:
        recency_days = 3650

    return {
        "needs_research": decision.needs_research,
        "mode": decision.mode,
        "queries": decision.queries,
        "recency_days": recency_days,
    }


def route_next(state: State):
    return "research" if state["needs_research"] else "orchestrator"


# ============================================================
# Research
# ============================================================


def _tavily_search(query: str, max_results: int = 5) -> List[dict]:
    if not os.getenv("TAVILY_API_KEY"):
        return []

    try:
        from langchain_community.tools.tavily_search import TavilySearchResults

        tool = TavilySearchResults(max_results=max_results)
        results = tool.invoke({"query": query})

        out = []

        for r in results or []:
            out.append(
                {
                    "title": r.get("title") or "",
                    "url": r.get("url") or "",
                    "snippet": r.get("content") or r.get("snippet") or "",
                    "published_at": r.get("published_date") or r.get("published_at"),
                    "source": r.get("source"),
                }
            )

        return out

    except Exception as e:
        print("Tavily error:", e)
        return []


RESEARCH_SYSTEM = """
You are a research synthesizer.

Given raw search results:
- Deduplicate by URL
- Keep concise snippets
- Never invent dates
- Return EvidenceItem schema only
"""


def _iso_to_date(s: Optional[str]):
    if not s:
        return None

    try:
        return date.fromisoformat(s[:10])
    except Exception:
        return None



def research_node(state: State) -> dict:
    queries = (state.get("queries") or [])[:10]

    raw = []

    for q in queries:
        raw.extend(_tavily_search(q, max_results=5))

    if not raw:
        return {"evidence": []}

    extractor = llm.with_structured_output(EvidencePack)

    pack = extractor.invoke(
        [
            SystemMessage(content=RESEARCH_SYSTEM),
            HumanMessage(content=f"Raw results:\n{raw}"),
        ]
    )

    dedup = {}

    for e in pack.evidence:
        if e.url:
            dedup[e.url] = e

    evidence = list(dedup.values())

    # safer filtering
    if state.get("mode") == "open_book":
        as_of = date.fromisoformat(state["as_of"])
        cutoff = as_of - timedelta(days=int(state["recency_days"]))

        filtered = []

        for e in evidence:
            d = _iso_to_date(e.published_at)

            if d is None:
                filtered.append(e)
            elif d >= cutoff:
                filtered.append(e)

        evidence = filtered

    print("FINAL EVIDENCE COUNT:", len(evidence))

    return {"evidence": evidence}


# ============================================================
# Orchestrator
# ============================================================

ORCH_SYSTEM = """
You are a senior technical writer.

Create a blog plan.

Requirements:
- 5-9 sections
- each section must contain:
  - title
  - goal
  - bullets
  - target_words

Avoid unnecessary LaTeX.
Use plain markdown.
"""



def orchestrator_node(state: State) -> dict:
    planner = llm.with_structured_output(Plan)

    plan = planner.invoke(
        [
            SystemMessage(content=ORCH_SYSTEM),
            HumanMessage(
                content=(
                    f"Topic: {state['topic']}\n"
                    f"Mode: {state['mode']}\n"
                    f"Evidence:\n{[e.model_dump() for e in state.get('evidence', [])][:15]}"
                )
            ),
        ]
    )

    return {"plan": plan}


# ============================================================
# Fanout
# ============================================================


def fanout(state: State):
    plan = state["plan"]

    return [
        Send(
            "worker",
            {
                "task": task.model_dump(),
                "plan": plan.model_dump(),
                "topic": state["topic"],
                "mode": state["mode"],
                "evidence": [e.model_dump() for e in state.get("evidence", [])],
            },
        )
        for task in plan.tasks
    ]


# ============================================================
# Worker
# ============================================================

WORKER_SYSTEM = """
You are a senior technical writer.

Write ONE markdown section.

Rules:
- Use proper markdown formatting
- Avoid raw LaTeX blocks like \[ \]
- Use readable explanations
- Include code snippets only if needed
- Start with ## Heading
"""



def worker_node(payload: dict):
    task = Task(**payload["task"])
    plan = Plan(**payload["plan"])

    evidence = [EvidenceItem(**e) for e in payload.get("evidence", [])]

    bullets = "\n".join([f"- {b}" for b in task.bullets])

    evidence_text = "\n".join(
        [f"- {e.title} | {e.url}" for e in evidence[:10]]
    )

    response = llm.invoke(
        [
            SystemMessage(content=WORKER_SYSTEM),
            HumanMessage(
                content=(
                    f"Blog title: {plan.blog_title}\n"
                    f"Topic: {payload['topic']}\n"
                    f"Section title: {task.title}\n"
                    f"Goal: {task.goal}\n"
                    f"Bullets:\n{bullets}\n\n"
                    f"Evidence:\n{evidence_text}"
                )
            ),
        ]
    )

    section_md = str(response.content).strip()

    return {"sections": [(task.id, section_md)]}


# ============================================================
# Reducer
# ============================================================


def _safe_slug(title: str):
    s = title.strip().lower()
    s = re.sub(r"[^a-z0-9 _-]+", "", s)
    s = re.sub(r"\s+", "_", s).strip("_")
    return s or "blog"



def merge_content(state: State):
    plan = state["plan"]

    ordered_sections = [
        md for _, md in sorted(state["sections"], key=lambda x: x[0])
    ]

    merged_md = f"# {plan.blog_title}\n\n" + "\n\n".join(ordered_sections)

    blog_slug = _safe_slug(plan.blog_title)
    images_dir = Path("images") / blog_slug
    images_dir.mkdir(parents=True, exist_ok=True)

    return {
        "merged_md": merged_md,
        "blog_slug": blog_slug,
        "images_dir": str(images_dir),
    }


DECIDE_IMAGES_SYSTEM = """
Decide if technical diagrams are needed.

Rules:
- Max 3 images
- Use placeholders [[IMAGE_1]] etc.
- Prefer technical diagrams
"""



def decide_images(state: State):
    planner = llm.with_structured_output(GlobalImagePlan)

    image_plan = planner.invoke(
        [
            SystemMessage(content=DECIDE_IMAGES_SYSTEM),
            HumanMessage(content=state["merged_md"]),
        ]
    )

    return {
        "md_with_placeholders": image_plan.md_with_placeholders,
        "image_specs": [img.model_dump() for img in image_plan.images],
    }



def _gemini_generate_image_bytes(prompt: str) -> bytes:
    """
    Returns raw image bytes generated by Gemini.
    Requires: pip install google-genai
    Env var: GOOGLE_API_KEY
    """
    from google import genai
    from google.genai import types

    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("GOOGLE_API_KEY is not set.")

    client = genai.Client(api_key=api_key)

    resp = client.models.generate_content(
    model="gemini-2.5-flash-image",
    contents=prompt,
    config=types.GenerateContentConfig(
        response_modalities=["IMAGE"],
        safety_settings=[
            types.SafetySetting(
                category=types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
                threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH,
            )
        ],
    ),
)

    # Depending on SDK version, parts may hang off resp.candidates[0].content.parts
    parts = getattr(resp, "parts", None)
    if not parts and getattr(resp, "candidates", None):
        try:
            parts = resp.candidates[0].content.parts
        except Exception:
            parts = None

    if not parts:
        raise RuntimeError("No image content returned (safety/quota/SDK change).")

    for part in parts:
        inline = getattr(part, "inline_data", None)
        if inline and getattr(inline, "data", None):
            return inline.data

    raise RuntimeError("No inline image bytes found in response.")



def generate_and_place_images(state: State):
    md = state.get("md_with_placeholders") or state["merged_md"]

    image_specs = state.get("image_specs", [])

    blog_slug = state["blog_slug"]
    images_dir = Path(state["images_dir"])

    for spec in image_specs:
        placeholder = spec["placeholder"]

        filename = Path(spec["filename"]).with_suffix(".png").name
        out_path = images_dir / filename

        try:
            if not out_path.exists():

                from PIL import Image
                from io import BytesIO

                img_bytes = _gemini_generate_image_bytes(spec["prompt"])
                
                if not img_bytes:
                    raise RuntimeError("No image bytes returned.")

                image = Image.open(BytesIO(img_bytes))

                out_path = out_path.with_suffix(".png")

                image.save(out_path, format="PNG")





            img_md = (
                f"![{spec['alt']}](images/{blog_slug}/{filename})\n"
                f"*{spec['caption']}*"
            )

            md = md.replace(placeholder, img_md)

        except Exception as e:
            md = md.replace(
                placeholder,
                f"> IMAGE GENERATION FAILED\n> {e}"
            )

    out_file = f"{blog_slug}.md"
    Path(out_file).write_text(md, encoding="utf-8")

    return {"final": md}


# ============================================================
# Reducer Graph
# ============================================================

reducer_graph = StateGraph(State)

reducer_graph.add_node("merge_content", merge_content)
reducer_graph.add_node("decide_images", decide_images)
reducer_graph.add_node("generate_and_place_images", generate_and_place_images)

reducer_graph.add_edge(START, "merge_content")
reducer_graph.add_edge("merge_content", "decide_images")
reducer_graph.add_edge("decide_images", "generate_and_place_images")
reducer_graph.add_edge("generate_and_place_images", END)

reducer_subgraph = reducer_graph.compile()


# ============================================================
# Main Graph
# ============================================================

g = StateGraph(State)

g.add_node("router", router_node)
g.add_node("research", research_node)
g.add_node("orchestrator", orchestrator_node)
g.add_node("worker", worker_node)
g.add_node("reducer", reducer_subgraph)

g.add_edge(START, "router")

g.add_conditional_edges(
    "router",
    route_next,
    {
        "research": "research",
        "orchestrator": "orchestrator",
    },
)

g.add_edge("research", "orchestrator")

g.add_conditional_edges("orchestrator", fanout)
g.add_edge("worker", "reducer")
g.add_edge("reducer", END)

app = g.compile()



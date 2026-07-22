"""
AI agent layer for the secondary modelling app.

Answers investor / analyst questions grounded in the numbers the finance
engine just computed (paid-in, DPI/RVPI/TVPI, IRR to date, the forward
distribution forecast, and the pricing sensitivity table). Uses Claude when
an API key is available; otherwise falls back to a deterministic templated
summary so the app still demos end-to-end without a key.
"""
import json
import os

SYSTEM_PROMPT = """You are a private equity secondaries analyst assistant embedded in a modelling tool.
You are given computed fund metrics: paid-in capital, distributions to date, DPI/RVPI/TVPI, IRR to
date, a year-by-year forecast of future distributions and ending NAV, and a secondary pricing
sensitivity table (buyer IRR/MOIC at various discounts or premiums to NAV).

Answer the user's question using ONLY the numbers in the provided context. Be precise and concise,
and cite the specific figures you rely on. If asked for a judgment call (e.g. "is this a good deal"),
give a reasoned view but flag the key assumptions (gross return, runoff shape, remaining life) and
risks driving it. Never invent numbers that are not present in the context."""


def _context_block(metrics: dict) -> str:
    return json.dumps(metrics, indent=2, default=str)


def ask_agent(question: str, metrics: dict, api_key: str = None, model: str = "claude-sonnet-5") -> str:
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=api_key)
            resp = client.messages.create(
                model=model,
                max_tokens=800,
                system=SYSTEM_PROMPT,
                messages=[{
                    "role": "user",
                    "content": f"Fund metrics context:\n{_context_block(metrics)}\n\nQuestion: {question}",
                }],
            )
            return resp.content[0].text
        except Exception as e:
            return f"_(AI agent call failed, showing a rule-based summary instead: {e})_\n\n" + _fallback_summary(metrics)
    return _fallback_summary(metrics) + "\n\n_Add an Anthropic API key in the sidebar to enable full natural-language Q&A._"


def _fallback_summary(metrics: dict) -> str:
    td = metrics.get("to_date", {})
    best = metrics.get("best_pricing", {})
    irr = td.get("irr")
    irr_txt = f"{irr*100:.1f}%" if irr == irr else "n/a"
    lines = [
        f"**To date:** paid-in ${td.get('paid_in', 0):,.0f}, distributions ${td.get('distributions', 0):,.0f}, "
        f"NAV ${td.get('nav', 0):,.0f}. TVPI {td.get('tvpi', 0):.2f}x, IRR to date {irr_txt}.",
    ]
    if best:
        b_irr = best.get("irr")
        b_irr_txt = f"{b_irr*100:.1f}%" if b_irr == b_irr else "n/a"
        lines.append(
            f"**Reference pricing:** at a {best.get('discount', 0)*100:.0f}% discount to NAV "
            f"(price ${best.get('price', 0):,.0f}), projected buyer IRR is {b_irr_txt} "
            f"with a {best.get('moic', 0):.2f}x MOIC."
        )
    return "\n\n".join(lines)

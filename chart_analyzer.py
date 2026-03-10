"""Analyze TradingView chart images to extract Take Profit levels using GPT-4o Vision."""

import base64
import logging
import os
from dataclasses import dataclass
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = "gpt-4o"

SYSTEM_PROMPT = """You are a trading chart analyzer. You receive TradingView chart screenshots from crypto traders.

Your job is to extract Take Profit (TP) price levels from the chart.

How to identify TP levels:
- They are horizontal lines BELOW the entry zone for SHORT trades, or ABOVE for LONG trades
- Usually shown as colored horizontal lines (cyan, blue, green, or dashed lines)
- The green shaded zone typically represents the profit target area
- TP levels go from closest to entry (TP1) to furthest (TP3/TP4)
- The stop loss is the red line on the opposite side of entry

Respond ONLY with a JSON object in this exact format:
{"direction": "LONG" or "SHORT", "entry_high": number, "entry_low": number, "stop_loss": number, "take_profits": [tp1, tp2, tp3, ...]}

Rules:
- Read prices from the Y-axis on the right side of the chart
- List take_profits from closest to entry to furthest
- If you can't determine exact prices, give your best estimate based on the chart grid
- If no take profits are visible, return an empty list
- Only include price levels that are clearly marked as targets, not random support/resistance"""


@dataclass
class ChartAnalysis:
    """Result of chart analysis."""
    direction: str  # LONG or SHORT
    entry_high: Optional[float] = None
    entry_low: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profits: list[float] = None

    def __post_init__(self):
        if self.take_profits is None:
            self.take_profits = []


async def analyze_chart_image(image_url: str = None, image_path: str = None) -> Optional[ChartAnalysis]:
    """Analyze a chart image and extract TP levels.

    Args:
        image_url: URL of the image to download and analyze
        image_path: Local file path of the image

    Returns:
        ChartAnalysis with extracted levels, or None on failure
    """
    if not OPENAI_API_KEY:
        logger.warning("CHART_ANALYZER: No OPENAI_API_KEY configured")
        return None

    try:
        # Get image as base64
        if image_path:
            with open(image_path, "rb") as f:
                image_data = base64.b64encode(f.read()).decode("utf-8")
            # Guess content type
            ext = image_path.lower().rsplit(".", 1)[-1]
            content_type = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg"}.get(ext, "image/png")
        elif image_url:
            async with aiohttp.ClientSession() as session:
                async with session.get(image_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        logger.warning("CHART_ANALYZER: Failed to download image: HTTP %d", resp.status)
                        return None
                    raw = await resp.read()
                    image_data = base64.b64encode(raw).decode("utf-8")
                    content_type = resp.content_type or "image/png"
        else:
            return None

        # Call GPT-4o Vision
        async with aiohttp.ClientSession() as session:
            payload = {
                "model": OPENAI_MODEL,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": "Extract the take profit levels from this trading chart.",
                            },
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:{content_type};base64,{image_data}",
                                    "detail": "high",
                                },
                            },
                        ],
                    },
                ],
                "max_tokens": 300,
                "temperature": 0,
            }

            headers = {
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            }

            async with session.post(
                "https://api.openai.com/v1/chat/completions",
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    error = await resp.text()
                    logger.error("CHART_ANALYZER: OpenAI API error %d: %s", resp.status, error[:200])
                    return None

                data = await resp.json()

        # Parse response
        content = data["choices"][0]["message"]["content"].strip()
        logger.info("CHART_ANALYZER: GPT-4o response: %s", content)

        # Extract JSON from response (handle markdown code blocks)
        import json
        if "```" in content:
            # Extract from code block
            json_str = content.split("```")[1]
            if json_str.startswith("json"):
                json_str = json_str[4:]
            json_str = json_str.strip()
        else:
            json_str = content

        # Fix thousand separators: "71,806.7" -> "71806.7"
        # Match digit,digit patterns (not inside quotes for keys)
        import re as _re
        json_str = _re.sub(r'(\d),(\d{3})', r'\1\2', json_str)
        # Handle cases like 71,806,700
        json_str = _re.sub(r'(\d),(\d{3})', r'\1\2', json_str)

        result = json.loads(json_str)

        return ChartAnalysis(
            direction=result.get("direction", "UNKNOWN"),
            entry_high=result.get("entry_high"),
            entry_low=result.get("entry_low"),
            stop_loss=result.get("stop_loss"),
            take_profits=result.get("take_profits", []),
        )

    except Exception as e:
        logger.error("CHART_ANALYZER: Error analyzing chart: %s", e)
        return None

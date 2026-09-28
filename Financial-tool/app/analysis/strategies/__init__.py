"""Strategy package. Importing it registers every built-in strategy."""
from app.analysis.strategies.base import (  # noqa: F401
    REGISTRY,
    Strategy,
    StrategyResult,
    get_strategy,
    list_strategies,
    register,
)
from app.analysis.strategies import (  # noqa: F401,E402
    bollinger_bands,
    ma_cross,
    macd_trend,
    momentum,
    rsi_reversion,
)

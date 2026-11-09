"""Avvia la campagna Spark per qwen3.8:27b."""

if __package__:
    from .spark_campaign import main
else:
    from spark_campaign import main


if __name__ == "__main__":
    raise SystemExit(main("qwen3.8:27b", "campagna-qwen-60.json"))

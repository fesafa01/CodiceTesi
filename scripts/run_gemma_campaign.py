"""Avvia la campagna Spark per gemma4:31b."""

if __package__:
    from .spark_campaign import main
else:
    from spark_campaign import main


if __name__ == "__main__":
    raise SystemExit(main("gemma4:31b", "campagna-gemma-60.json"))

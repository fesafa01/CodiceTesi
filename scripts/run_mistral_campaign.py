"""Avvia la campagna Spark per frob/mistral-small-4:119b."""

if __package__:
    from .spark_campaign import main
else:
    from spark_campaign import main


if __name__ == "__main__":
    raise SystemExit(main("frob/mistral-small-4:119b", "campagna-mistral-60.json"))

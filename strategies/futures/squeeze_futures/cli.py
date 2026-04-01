import typer


app = typer.Typer(help="Squeeze Taiwan futures utilities.")


@app.command()
def ping():
    """Simple smoke-test entry point."""
    typer.echo("squeeze-futures CLI is available")


if __name__ == "__main__":
    app()

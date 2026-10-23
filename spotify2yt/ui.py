from __future__ import annotations

from rich.console import Console
from rich.markup import escape
from rich.table import Table

console = Console()


def ok(message: str) -> None:
    console.print(f"[green]OK[/] {escape(str(message))}")


def warn(message: str) -> None:
    console.print(f"[yellow]![/] {escape(str(message))}")


def err(message: str) -> None:
    console.print(f"[red]x[/] {escape(str(message))}")


def info(message: str) -> None:
    console.print(f"[cyan]i[/] {escape(str(message))}")


def dim(message: str) -> None:
    console.print(f"[dim]{escape(str(message))}[/]")


def playlist_table(rows: list[dict]) -> Table:
    table = Table(title="Spotify playlists", show_lines=False)
    table.add_column("#", justify="right", style="dim")
    table.add_column("Name", style="bold")
    table.add_column("Tracks", justify="right")
    table.add_column("Owner")
    table.add_column("Spotify ID", style="dim")
    for i, row in enumerate(rows, 1):
        table.add_row(
            str(i),
            escape(str(row.get("name") or "?")),
            str(row.get("total") or 0),
            escape(str(row.get("owner") or "")),
            escape(str(row.get("id") or "")),
        )
    return table

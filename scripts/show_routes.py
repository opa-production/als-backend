"""
Prints every route Swagger will expose, so surprises happen here.

Reads the OpenAPI schema rather than ``app.routes``: recent FastAPI resolves
an included router lazily, so the routes list can be empty long after
``include_router`` has been called. The schema is what the docs actually show,
which makes it the honest source.
"""

from app.main import app


def main() -> None:
    schema = app.openapi()
    rows = [
        (method.upper(), path, operation.get("summary", ""))
        for path, methods in sorted(schema.get("paths", {}).items())
        for method, operation in methods.items()
    ]

    for method, path, summary in sorted(rows, key=lambda r: (r[1], r[0])):
        print(f"  {method:<7} {path:<28} {summary}")

    print(f"\n{len(rows)} route(s)")


if __name__ == "__main__":
    main()

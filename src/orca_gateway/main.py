from fastapi import FastAPI

app = FastAPI(title="orca-gateway")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}

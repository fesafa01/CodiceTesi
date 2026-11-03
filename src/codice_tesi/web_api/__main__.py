"""Avvio single-worker: lo stato dei lavori attivi è coordinato nel processo."""

import uvicorn

uvicorn.run("codice_tesi.web_api.app:create_app", factory=True, host="0.0.0.0", port=8002)

# Tokenizer DeepSeek V4

`deepseek_v4.json` è il file `tokenizer.json` ufficiale, senza modifiche:

- Repository: https://huggingface.co/deepseek-ai/DeepSeek-V4-Flash
- Revisione: `60d8d70770c6776ff598c94bb586a859a38244f1`
- SHA-256: `8f9f37ca37fdc4f5fd36d5cf4d3b0e8392edb4e894fd10cc0d70b4957c8633cf`
- Licenza: MIT, conservata in `LICENSE.deepseek`.

Il template per i due messaggi testuali e il prefisso di reasoning massimo seguono
`encoding/encoding_dsv4.py` della medesima revisione. Il flusso non usa tool,
immagini o cronologia. Gli ID Flash e PRO sono alias compatibili del server DS4;
il tokenizer locale non verifica quale GGUF sia caricato sul server.

I file sono inclusi nella distribuzione Python e nelle immagini Docker. Non vengono
scaricati tokenizer o pesi durante la preparazione delle richieste. Dopo un cambio
di tokenizer o template sul server occorre verificare nuovamente il confronto con
`usage.prompt_tokens`; il margine applicativo resta distinto dal conteggio locale.

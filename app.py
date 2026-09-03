import os
import json
import base64

def obter_credenciais():
    creds_b64 = os.environ.get("GOOGLE_CREDENTIALS")
    if creds_b64:
        # Se for Base64, decodifica
        try:
            creds_json = json.loads(base64.b64decode(creds_b64).decode("utf-8"))
            return creds_json
        except Exception:
            # Se falhar, tenta interpretar como JSON puro
            try:
                return json.loads(creds_b64)
            except Exception:
                raise Exception("GOOGLE_CREDENTIALS inválida. Deve ser JSON ou Base64.")
    else:
        raise Exception("GOOGLE_CREDENTIALS não definida no ambiente.")

# No seu código, use:
CREDS_JSON = obter_credenciais()
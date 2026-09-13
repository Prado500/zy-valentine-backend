# PENDIENTE · Publicar los textos legales v1.1 en el backend

**Levantado el 2026-09-13.** No resuelto a propósito: el hallazgo salió trabajando en
`fe/` y el arreglo vive aquí. Este documento deja todo medido para que se ejecute sin
volver a investigar.

---

## 1. El problema

El modal de registro/compra **no lee los `.md` del frontend**. Pide el texto a esta API:

```
GET /api/v1/public/legal/terms  →  { version, checksum, content }
```

Y está bien que así sea: `app/legal/__init__.py` lo explica — el consentimiento guarda el
**checksum** del texto aceptado, y ese checksum solo prueba algo si lo calcula quien lo
guarda.

El problema es **qué** se sirve hoy. `app/legal/terms_v1.md` es una plantilla sin rellenar:

```
$ grep -o "\[\[[^]]*\]\]" app/legal/terms_v1.md | sort -u
[[CORREO DE CONTACTO]]
[[DIRECCIÓN]]
[[NIT]]
[[PLAZO]]
[[RAZÓN SOCIAL]]
```

**Cada persona que se registra está aceptando hoy un documento con marcadores de plantilla
literales**, en 11 secciones, versión `2026-09-10`. Mientras tanto, `/terminos` y
`/privacidad` en el sitio publican la v1.1 real, de 27 secciones. Se acepta un texto y se
publica otro.

---

## 2. El texto exacto que hay que servir

**`docs/legal/terms_and_privacy_v1.1.md`**, en este mismo directorio. Ya está generado y
verificado.

| | |
|---|---|
| Bytes | 30 347 |
| Líneas | 371 |
| Finales de línea | LF |
| `sha256` | `c9a421396b362b763eb3bc6c2570d50437b37694a83e5643b35d17e4b28dc861` |

Ese `sha256` es el que `TERMS_CHECKSUM` debe calcular. Si sale otro, el archivo se alteró
al copiarlo (casi siempre: CRLF).

### De dónde sale, y por qué importa

Es la **concatenación byte a byte** de los dos documentos publicados en el frontend:

```
fe/src/modules/legal/content/terminos-y-condiciones.md
    + "\n\n---\n\n" +
fe/src/modules/legal/content/politica-de-privacidad.md
```

Un solo documento porque `TERMS_KIND = "terms_and_privacy"` ya declara que aquí van los
dos juntos.

> ⚠️ **La invariante que no se puede romper:** lo que sirve esta API tiene que ser
> idéntico a lo que publican `/terminos` y `/privacidad`. Si alguien edita uno de los tres
> sin los otros dos, vuelve a haber dos versiones del mismo texto — que es exactamente lo
> que el checksum existe para impedir. Regenerar, no editar a mano:
>
> ```python
> tyc = (BASE / 'terminos-y-condiciones.md').read_text(encoding='utf-8')
> pri = (BASE / 'politica-de-privacidad.md').read_text(encoding='utf-8')
> norm = lambda t: t.replace('\r\n', '\n').replace('\r', '\n').strip('\n')
> combinado = norm(tyc) + '\n\n---\n\n' + norm(pri) + '\n'
> ```

---

## 3. Los dos cambios de código

### 3.1 Reemplazar el contenido de `app/legal/terms_v1.md`

**Reemplazar el contenido, no renombrar el archivo.** `test_legal_text_travels_with_the_package`
afirma que existe `app/legal/terms_v1.md` y que `pyproject.toml` declara `"app.legal"` en
`package-data`. Renombrarlo rompe esa prueba y, peor, rompe una instalación normal donde el
`.md` no viaja con el paquete.

### 3.2 Subir la versión en `app/legal/__init__.py`

```python
TERMS_VERSION = "1.1"    # era "2026-09-10"
```

**Tiene que ser exactamente `"1.1"`.** No una fecha. Lo comprobé:

| Candidato | ¿Aparece en el texto? |
|---|---|
| `"1.1"` | ✅ — «**Versión:** 1.1» en ambos documentos |
| `"2026-09-12"` | ❌ el texto dice «12 de septiembre de 2026», no la fecha ISO |
| `"2026-09-10"` | ❌ |

`test_version_is_declared_and_matches_the_text` afirma `TERMS_VERSION in TERMS_TEXT`, así
que cualquier formato de fecha falla. Si se quiere conservar la convención por fecha, hay
que añadir la fecha ISO al cuerpo del `.md` — y entonces deja de ser byte-idéntico al del
frontend, y se pierde la invariante de §2. **Recomendado: `"1.1"`.**

`TERMS_CHECKSUM` se recalcula solo; no hay nada que tocar ahí.

---

## 4. La prueba que hay que actualizar

`tests/test_legal_terms.py::test_the_two_critical_clauses_are_present`

```python
def test_the_two_critical_clauses_are_present():
    lowered = legal.TERMS_TEXT.lower()
    assert "fotograf" in lowered
    assert "carta html" in lowered      # ← esta falla
```

| Cláusula | En el texto nuevo |
|---|---|
| `"fotograf"` | ✅ pasa |
| `"carta html"` | ❌ **falla** |

El texto v1.1 dice **«archivo HTML»** —11 veces— y nunca «carta HTML». La prueba vigila que
la cláusula del entregable HTML exista, y existe: lo que cambió es cómo se nombra. La
corrección correcta es actualizar la frase buscada, no quitar la comprobación:

```python
    assert "archivo html" in lowered
```

No he encontrado ninguna otra prueba que dependa del contenido. Las demás usan
`legal.TERMS_VERSION` por referencia y siguen valiendo.

---

## 5. Qué pasa el día del despliegue

`attach_consent` rechaza con **422 `TERMS_VERSION_MISMATCH`** cualquier registro que envíe
una versión distinta de la vigente:

> «Los términos cambiaron. Recarga la página y vuelve a intentarlo.»

Es el comportamiento correcto y ya está contemplado, pero significa que **quien tenga el
formulario de registro abierto en el momento del despliegue verá ese error y tendrá que
recargar**. Si eso importa, desplegar en hora valle.

Los consentimientos ya firmados conservan su `document_version` y su `document_checksum`
antiguos. Es lo que debe pasar —cada quien aceptó lo que aceptó— pero conviene saberlo:
después de esto habrá consentimientos apuntando a un texto que la API ya no sirve. Si se
necesita poder reconstruir lo que firmó cada persona, hay que **conservar el `.md` anterior
en el repositorio**, no sobrescribirlo y ya. Ese archivo no existe hoy como histórico.

---

## 6. Cómo verificar que quedó bien

```bash
# 1. El checksum es el esperado
python -c "from app import legal; print(legal.TERMS_CHECKSUM)"
#    → c9a421396b362b763eb3bc6c2570d50437b37694a83e5643b35d17e4b28dc861

# 2. No quedan marcadores de plantilla
grep -o "\[\[[^]]*\]\]" app/legal/terms_v1.md ; echo "(vacío = bien)"

# 3. Las pruebas
pytest tests/test_legal_terms.py tests/test_registration_compliance.py tests/test_auth.py

# 4. Y el texto servido es el mismo que publica el sitio
python -c "
from pathlib import Path
from app import legal
ref = Path('docs/legal/terms_and_privacy_v1.1.md').read_text(encoding='utf-8')
ref = ref.replace(chr(13)+chr(10), chr(10)).replace(chr(13), chr(10))
print('IGUALES' if ref == legal.TERMS_TEXT else 'DIFIEREN')
"
```

> **Por qué ese `python` y no un `diff` a secas:** este repositorio tiene
> `core.autocrlf=true` y no hay `.gitattributes`, así que en Windows el `.md` sale del
> checkout con CRLF mientras que `TERMS_TEXT` está normalizado a LF. Un `diff` directo
> diría que diferen cuando son el mismo texto. El checksum **no** se ve afectado —
> `app/legal/__init__.py` normaliza antes de hashear, que es justo para esto.

Y al final, a ojo: abrir el modal de registro y comprobar que ya no aparece ningún
`[[MARCADOR]]`, que el pie del documento dice **Versión 1.1** y que se leen las dos partes,
Términos y Política de Privacidad, una detrás de otra.

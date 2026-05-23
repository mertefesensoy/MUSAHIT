import asyncio
from musahit.common.db import open_connection
from musahit.tts.piper import PiperPythonClient
from musahit.tts.synthesizer import Synthesizer

async def main():
    with open_connection("data/musahit.duckdb") as conn:
        conn.execute("""
            INSERT OR REPLACE INTO pipeline_runs
                (run_id, started_at, status, stages_done, counts)
            VALUES
                ('run_20260523', NOW(), 'RUNNING', '["write"]', '{}')
        """)
        conn.execute("""
            INSERT OR REPLACE INTO briefings
                (date, generated_at, markdown_path, html_path)
            VALUES
                ('2026-05-23', NOW(),
                 'briefings/2026/05/23/briefing.md',
                 'briefings/2026/05/23/briefing.html')
        """)

        piper = PiperPythonClient(
            voice_path="C:/Users/senso/AppData/Local/piper/voices/tr_TR-dfki-medium.onnx"
        )
        synth = Synthesizer(conn, piper, briefings_root="briefings")
        await synth.run("run_20260523")

    print("synth ok · open briefings/2026/05/23/briefing.mp3")

asyncio.run(main())

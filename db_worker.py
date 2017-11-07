import logging
import psycopg2
import psycopg2.extras
import os
import time
import typing
import asyncio
import aiohttp
import async_timeout

class DBTodoList:
    EXPECTED_VERSION = 1

    def __init__(
        self,
        host: str,
        port: int,
        dbname: str,
        user: str,
        password: str,
        root_user: str,
        root_password: str
    ):
        self.conn = psycopg2.connect(
            host=host,
            port=port,
            dbname=dbname,
            user=user,
            password=password)

        version = self.get_schema_version()

        # bring us up to version 1
        if version < 1:
            root_conn = psycopg2.connect(
                    host=host,
                    port=port,
                    dbname=dbname,
                    user=root_user,
                    password=root_password)
            try:
                with root_conn, root_conn.cursor() as root_cur:
                    logging.info("Updating database to version 1")

                    root_cur.execute("CREATE TABLE settings (key VARCHAR PRIMARY KEY, value VARCHAR);")

                    root_cur.execute("GRANT SELECT ON settings TO %s;" % user)

                    root_cur.execute("""
                        CREATE TYPE processing_status AS ENUM (
                          'Scheduled',
                          'Processing',
                          'Done',
                          'Failed'
                        );
                    """)

                    root_cur.execute("""
                        CREATE TABLE tasks (
                          modelVersion SMALLINT NOT NULL,
                          paperId CHAR(40) NOT NULL,
                          status processing_status NOT NULL DEFAULT 'Scheduled'::processing_status,
                          statusChanged TIMESTAMP NOT NULL DEFAULT NOW(),
                          attempts SMALLINT NOT NULL DEFAULT 0,
                          result JSONB,
                          PRIMARY KEY (modelVersion, paperId)
                        );
                    """)

                    root_cur.execute("GRANT SELECT, INSERT, UPDATE ON tasks TO %s;" % user)

                    root_cur.execute("""
                        CREATE FUNCTION update_status_changed_trigger() RETURNS TRIGGER AS $$
                          BEGIN
                              NEW.statusChanged := NOW();
                              RETURN NEW;
                          END
                        $$ LANGUAGE plpgsql;
                    """)

                    root_cur.execute("""
                        CREATE TRIGGER update_status_changed
                        BEFORE UPDATE OF status ON tasks
                        FOR EACH ROW
                        EXECUTE PROCEDURE update_status_changed_trigger();
                    """)

                    root_cur.execute("""
                        CREATE FUNCTION effective_status_fn (
                          status processing_status,
                          statusChanged TIMESTAMP,
                          attempts SMALLINT
                        ) RETURNS processing_status
                        RETURNS NULL ON NULL INPUT
                        AS
                        $$
                        SELECT CASE
                          WHEN
                            -- scheduled more than five times
                            status = 'Scheduled'::processing_status AND
                            attempts >= 5
                            THEN 'Failed'::processing_status
                          WHEN
                            -- processing expired, and scheduled more than five times
                            status = 'Processing'::processing_status AND
                            statusChanged + interval '5 minutes' < NOW() AND
                            attempts >= 5
                            THEN 'Failed'::processing_status
                          WHEN
                            -- processing expired
                            status = 'Processing'::processing_status AND
                            statusChanged + interval '5 minutes' < NOW()
                            THEN 'Scheduled'::processing_status                        
                          ELSE status 
                        END
                        $$ LANGUAGE SQL IMMUTABLE;
                    """)

                    root_cur.execute("""
                        CREATE VIEW tasks_with_status AS
                          SELECT *, effective_status_fn(status, statusChanged, attempts) 
                          AS effectiveStatus
                          FROM tasks;
                    """)

                    root_cur.execute("""
                        CREATE INDEX ON tasks(paperid) WHERE
                            status = 'Scheduled'::processing_status OR
                            status = 'Processing'::processing_status;
                    """)

                    root_cur.execute("GRANT SELECT, INSERT, UPDATE ON tasks_with_status TO %s;" % user)

                    # set the version number
                    root_cur.execute("INSERT INTO settings (key, value) VALUES ('version', 1);")
                    root_conn.commit()
                    version = 1
            finally:
                root_conn.close()

        logging.info("Database is at version %d", version)

    def get_schema_version(self):
        try:
            with self.conn.cursor() as cur:
                cur.execute("SELECT value FROM settings WHERE key = 'version'")
                return int(cur.fetchone()[0])
        except psycopg2.ProgrammingError as e:
            if 'relation "settings" does not exist' in str(e):
                self.conn.rollback()
                return 0
            else:
                raise

    def get_batch_to_process(self, model_version: int, max_batch_size: int=100):
        try:
            with self.conn.cursor() as cur:
                cur.execute("""
                    WITH selected AS (
                      SELECT modelversion, paperid
                      FROM tasks_with_status
                      WHERE
                        ( -- not necessary, but should result in better use of the index
                          status = 'Scheduled'::PROCESSING_STATUS OR
                          status = 'Processing'::PROCESSING_STATUS
                        ) AND
                        effectivestatus = 'Scheduled'::processing_status AND
                        modelversion = %s
                      LIMIT %s
                      FOR UPDATE SKIP LOCKED
                    )
                    UPDATE tasks_with_status ts
                    SET
                      status = 'Processing'::processing_status,
                      attempts = ts.attempts + 1
                    FROM selected
                    WHERE
                      selected.paperid = ts.paperid AND
                      selected.modelversion = ts.modelversion
                    RETURNING ts.paperid
                """, (model_version, max_batch_size))

                paper_ids = cur.fetchall()
            self.conn.commit()
        except:
            self.conn.rollback()
            raise

        return [x[0] for x in paper_ids]

    def post_results(self, model_version: int, paper_id_to_result):
        try:
            with self.conn.cursor() as cur:
                psycopg2.extras.execute_batch(
                    cur,
                    """INSERT INTO tasks (modelversion, paperid, status, attempts, result)
                    VALUES (%s, %s, 'Done'::processing_status, 1, %s)
                    ON CONFLICT (modelversion, paperid) DO UPDATE SET 
                      status = EXCLUDED.status,
                      result = EXCLUDED.result""",
                    [
                        (model_version, pid, psycopg2.extras.Json(result))
                        for pid, result in paper_id_to_result.items()
                    ]
                )
            self.conn.commit()
        except:
            self.conn.rollback()
            raise

    def post_errors(self, model_version: int, paper_id_to_error):
        try:
            with self.conn.cursor() as cur:
                psycopg2.extras.execute_batch(
                    cur,
                    """INSERT INTO tasks (modelversion, paperid, status, attempts, result)
                    VALUES (%s, %s, 'Scheduled'::processing_status, 1, %s)
                    ON CONFLICT (modelversion, paperid) DO UPDATE SET 
                      status = EXCLUDED.status,
                      result = EXCLUDED.result""",
                    [
                        (model_version, pid, psycopg2.extras.Json(result))
                        for pid, result in paper_id_to_error.items()
                    ]
                )
            self.conn.commit()
        except:
            self.conn.rollback()
            raise

def _send_all(source, dest, nbytes: int = None):
    nsent = 0
    while nbytes is None or nsent < nbytes:
        tosend = 64 * 1024
        if nbytes is not None:
            tosend = min(tosend, nbytes - nsent)
        buf = source.read(tosend)
        if not buf:
            break
        dest.write(buf)
        nsent += len(buf)
    dest.flush()

def _sanitize_for_json(s: typing.Optional[str]) -> typing.Optional[str]:
    if s is not None:
        return s.replace("\0", "\ufffd")
    else:
        return None

def main():
    import tempfile
    import argparse
    import http.client
    import h5py

    import settings
    import dataprep2

    if os.name != 'nt':
        import manhole
        manhole.install()

    logging.getLogger().setLevel(logging.DEBUG)

    default_password = os.environ.get("SPV2_PASSWORD")
    default_root_password = os.environ.get("SPV2_ROOT_PASSWORD")
    default_dataprep_host = os.environ.get("SPV2_DATAPREP_SERVICE_HOST", "localhost")
    default_dataprep_port = int(os.environ.get("SPV2_DATAPREP_SERVICE_PORT", "8080"))

    parser = argparse.ArgumentParser(description="Trains a classifier for PDF Tokens")
    parser.add_argument(
        "--host",
        type=str,
        default="localhost",
        help="database host"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=5432,
        help="database port"
    )
    parser.add_argument(
        "--dbname",
        type=str,
        default="spv2",
        help="database name"
    )
    parser.add_argument(
        "--user",
        type=str,
        default="spv2",
        help="database user"
    )
    parser.add_argument(
        "--password",
        type=str,
        default=default_password,
        help="database password"
    )
    parser.add_argument(
        "--root-user",
        type=str,
        default="root",
        help="database user"
    )
    parser.add_argument(
        "--root-password",
        type=str,
        default=default_root_password,
        help="database password"
    )
    parser.add_argument(
        "--dataprep-host",
        type=str,
        default=default_dataprep_host,
        help="Host where the dataprep service is running"
    )
    parser.add_argument(
        "--dataprep-port",
        type=str,
        default=default_dataprep_port,
        help="Port where the dataprep service is running"
    )
    args = parser.parse_args()

    todo_list = DBTodoList(
        host = args.host,
        port = args.port,
        dbname = args.dbname,
        user = args.user,
        password = args.password,
        root_user = args.root_user,
        root_password = args.root_password
    )

    logging.info("Loading model settings ...")
    model_settings = settings.default_model_settings

    logging.info("Loading token statistics ...")
    token_stats = dataprep2.TokenStatistics("model/all.tokenstats3.gz")

    logging.info("Loading embeddings ...")
    embeddings = dataprep2.CombinedEmbeddings(
        token_stats,
        dataprep2.GloveVectors(model_settings.glove_vectors),
        model_settings.minimum_token_frequency
    )

    import with_labels  # Heavy import, so we do it here
    model = with_labels.model_with_labels(model_settings, embeddings)
    model.load_weights("model/B40.h5")
    model_version = 1

    # async http stuff
    async_event_loop = asyncio.get_event_loop()
    session = aiohttp.ClientSession(loop = async_event_loop, read_timeout=60, conn_timeout=60)
    write_lock = asyncio.Lock()
    async def write_json_tokens_to_file(paper_id: str, json_file):
        url = "http://%s:%d/v1/json/paperid/%s" % (args.dataprep_host, args.dataprep_port, paper_id)
        attempts_left = 5
        while True:
            attempts_left -= 1
            try:
                async with session.get(url) as response:
                    if response.status == 200:
                        with await write_lock:
                            while True:
                                chunk = await response.content.read(1024 * 1024)
                                if not chunk:
                                    break
                                json_file.write(chunk)
                            break
                    else:
                        if attempts_left > 0:
                            logging.error(
                                "Error %d from dataprep server for paper id %s. %d attempts left.",
                                response.status,
                                paper_id,
                                attempts_left)
                        else:
                            logging.error(
                                "Error %d from dataprep server for paper id %s. Dropping paper.",
                                response.status,
                                paper_id)
                            break
            except Exception as e:
                if attempts_left > 0:
                    logging.error(
                        "Error %s from dataprep server for paper id %s. %d attempts left.",
                        e,
                        paper_id,
                        attempts_left)
                else:
                    logging.error(
                        "Error %s from dataprep server for paper id %s. Dropping paper.",
                        e,
                        paper_id)
                    break

    logging.info("Starting to process tasks")
    total_paper_ids_processed = 0
    start_time = time.time()
    last_time_with_paper_ids = start_time
    processing_timeout = 600
    while True:
        paper_ids = todo_list.get_batch_to_process(model_version)
        logging.info("Received %d paper ids", len(paper_ids))
        if len(paper_ids) <= 0:
            if time.time() - last_time_with_paper_ids > processing_timeout:
                logging.info("Saw no paper ids for more than %.0f seconds. Shutting down.", processing_timeout)
                return
            time.sleep(20)
            continue

        with tempfile.TemporaryDirectory(prefix="SPV2DBWorker-") as temp_dir:
            # make JSON out of the papers
            logging.info("Getting JSON ...")
            getting_json_time = time.time()
            json_file_name = os.path.join(temp_dir, "tokens.json")
            with open(json_file_name, "wb") as json_file:
                write_json_futures = [write_json_tokens_to_file(p, json_file) for p in paper_ids]
                async_event_loop.run_until_complete(asyncio.wait(write_json_futures))
            getting_json_time = time.time() - getting_json_time
            logging.info("Got JSON in %.2f seconds", getting_json_time)

            # pick out errors and write them to the DB
            paper_id_to_error = {}
            for line in dataprep2.json_from_file(json_file_name):
                if not "error" in line:
                    continue
                error = line["error"]
                error["message"] = _sanitize_for_json(error["message"])
                error["stackTrace"] = _sanitize_for_json(error["stackTrace"])
                paper_id = error["docName"]
                if paper_id.endswith(".pdf"):
                    paper_id = paper_id[:-4]
                paper_id_to_error[paper_id] = error
                logging.info("Paper %s has error %s", paper_id, error["message"])
            todo_list.post_errors(model_version, paper_id_to_error)
            logging.info("Wrote %d errors to database", len(paper_id_to_error))

            # make unlabeled tokens file
            logging.info("Making unlabeled tokens ...")
            making_unlabeled_tokens_time = time.time()
            unlabeled_tokens_file_name = os.path.join(temp_dir, "unlabeled-tokens.h5")
            dataprep2.make_unlabeled_tokens_file(
                json_file_name,
                unlabeled_tokens_file_name,
                ignore_errors=True)
            os.remove(json_file_name)
            making_unlabeled_tokens_time = time.time() - making_unlabeled_tokens_time
            logging.info("Made unlabeled tokens in %.2f seconds", making_unlabeled_tokens_time)

            # make featurized tokens file
            logging.info("Making featurized tokens ...")
            making_featurized_tokens_time = time.time()
            with h5py.File(unlabeled_tokens_file_name, "r") as unlabeled_tokens_file:
                featurized_tokens_file_name = os.path.join(temp_dir, "featurized-tokens.h5")
                dataprep2.make_featurized_tokens_file(
                    featurized_tokens_file_name,
                    unlabeled_tokens_file,
                    token_stats,
                    embeddings,
                    dataprep2.VisionOutput(None),   # TODO: put in real vision output
                    model_settings
                )
                # We don't delete the unlabeled file here because the featurized one contains references
                # to it.
            making_featurized_tokens_time = time.time() - making_featurized_tokens_time
            logging.info("Made featurized tokens in %.2f seconds", making_featurized_tokens_time)

            logging.info("Making and sending results ...")
            make_and_send_results_time = time.time()
            with h5py.File(featurized_tokens_file_name) as featurized_tokens_file:
                def get_docs():
                    return dataprep2.documents_for_featurized_tokens(
                        featurized_tokens_file,
                        include_labels=False,
                        max_tokens_per_page=model_settings.tokens_per_batch)
                results = with_labels.run_model(model, model_settings, get_docs)
                results = {
                    doc.doc_sha: {
                        "docName": doc.doc_id,
                        "docSha": doc.doc_sha,
                        "title": _sanitize_for_json(title),
                        "authors": authors
                    } for doc, title, authors in results
                }

                todo_list.post_results(model_version, results)
                total_paper_ids_processed += len(results)

            make_and_send_results_time = time.time() - make_and_send_results_time
            logging.info("Made and sent results in %.2f seconds", make_and_send_results_time)

        # report progress
        paper_ids_per_hour = 3600 * total_paper_ids_processed / (time.time() - start_time)
        logging.info("This worker is processing %.0f paper ids per hour." % paper_ids_per_hour)

        last_time_with_paper_ids = time.time()

if __name__ == "__main__":
    main()
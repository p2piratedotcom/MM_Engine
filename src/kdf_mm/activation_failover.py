"""Service-owned EVM activation, one configured endpoint per attempt.

Only a confirmed terminal KDF error permits another activation. A lost local
RPC response is not evidence of failure and must never create a second task.
"""
from __future__ import annotations

import copy
import threading
import time
from typing import Any


class EvmActivationFailover:
    def __init__(self, kdf, *, interval: float = 2.0, background: bool = True):
        self.kdf = kdf
        self.interval = interval
        self.background = background
        self.lock = threading.RLock()
        self.step_lock = threading.Lock()
        self.tasks: dict[int, dict[str, Any]] = {}

    def start(self, params):
        with self.lock:
            for task_id, job in self.tasks.items():
                if job['params']['ticker'] == params['ticker'] and not job['done']:
                    if job['params'] != params:
                        raise ValueError('Attivazione già in corso per questa piattaforma; attendere prima di aggiungere token')
                    return {'task_id': task_id}
            single = copy.deepcopy(params)
            single['nodes'] = [single['nodes'][0]]
            response = self.kdf.enable_evm_with_tokens(single)
            task_id = int(response['task_id'])
            self.tasks[task_id] = {
                'params': copy.deepcopy(params), 'current': task_id, 'index': 0,
                'failures': [], 'done': False,
                'result': {'status': 'InProgress', 'details': 'Attivazione in corso sul nodo 1'},
            }
            if self.background:
                threading.Thread(target=self._run, args=(task_id,), daemon=True,
                                 name=f'evm-activation-{task_id}').start()
            return response

    def status(self, task_id):
        with self.lock:
            job = self.tasks.get(task_id)
            return copy.deepcopy(job['result']) if job else None

    def snapshot(self):
        with self.lock:
            latest = {}
            for task_id, job in self.tasks.items():
                latest[job['params']['ticker']] = {
                    'task_id': task_id, **copy.deepcopy(job['result'])}
            return latest

    def _run(self, task_id):
        while True:
            if self.step(task_id):
                return
            time.sleep(self.interval)

    def step(self, task_id):
        """One polling step; exposed for deterministic, network-free tests."""
        with self.step_lock:
            job = self.tasks[task_id]
            if job['done']:
                return True
            if job['result'].get('status') == 'Unknown':
                return True
            try:
                result = self.kdf.enable_evm_with_tokens_status(
                    job['current'], forget_if_finished=False)
            except Exception:
                job['result'] = {'status': 'InProgress', 'details':
                    'KDF locale non raggiungibile: esito da verificare, nessuna nuova attivazione inviata.'}
                return False
            if result.get('status') != 'Error':
                job['result'] = result
                job['done'] = result.get('status') in {'Ok', 'Cancelled'}
                return job['done']
            nodes = job['params']['nodes']
            job['failures'].append({'node': nodes[job['index']]['url'],
                                    'cause': result.get('details', 'Errore KDF')})
            job['index'] += 1
            if job['index'] == len(nodes):
                job['result'] = {'status': 'Error', 'details': {
                    'error': f"Attivazione {job['params']['ticker']} fallita: tutti i {len(nodes)} nodi configurati hanno fallito.",
                    'attempts': copy.deepcopy(job['failures'])}}
                job['done'] = True
                return True
            params = copy.deepcopy(job['params'])
            params['nodes'] = [nodes[job['index']]]
            try:
                response = self.kdf.enable_evm_with_tokens(params)
                job['current'] = int(response['task_id'])
            except Exception:
                # The init may have reached KDF. Never blindly resend it.
                job['result'] = {'status': 'Unknown', 'details':
                    'Esito del nuovo tentativo non disponibile. Verificare KDF prima di ripetere; non è confermato un guasto di tutti i nodi.'}
                return True
            job['result'] = {'status': 'InProgress', 'details':
                f"Nodo precedente fallito; tentativo {job['index'] + 1}/{len(nodes)} su {nodes[job['index']]['url']}"}
            return False

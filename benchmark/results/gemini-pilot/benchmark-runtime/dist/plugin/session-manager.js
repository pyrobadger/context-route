export class SessionManager {
    pending = new Map();
    active = new Map();
    admissions = new Set();
    add(task) {
        this.pending.set(task.messageId, task);
    }
    get(session) {
        return this.active.get(session);
    }
    busy(session) {
        return this.active.get(session)?.finished === false
            || Array.from(this.pending.values()).some(task => task.sessionId === session);
    }
    deliver(session, message) {
        const task = this.pending.get(message);
        if (task) {
            task.delivered = true;
            this.pending.delete(message);
            this.active.set(session, task);
        }
        return task;
    }
    latest(session) {
        return this.active.get(session)
            ?? Array.from(this.pending.values()).find(task => task.sessionId === session);
    }
    remove(session) {
        this.active.delete(session);
        for (const [id, task] of this.pending)
            if (task.sessionId === session)
                this.pending.delete(id);
    }
    cancel(message) {
        this.pending.delete(message);
    }
}

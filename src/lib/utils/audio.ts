type AudioQueueEvent = 'stop' | 'empty-queue' | 'id-change';

interface AudioQueueStopDetail {
	event: AudioQueueEvent;
	id: string | null;
}

export type OnStoppedCallback = (detail: AudioQueueStopDetail) => void;

export interface AudioQueueMetadata {
	title: string;
	artist?: string;
	album?: string;
}

const MEDIA_SESSION_ACTIONS = ['play', 'pause', 'stop', 'seekbackward', 'seekforward'] as const;

export class AudioQueue {
	private audio: HTMLAudioElement;
	private queue: string[] = [];
	private current: string | null = null;
	private readonly _onEnded = () => this.next();
	private readonly _onVisibilityChange = () => {
		// Wake lock auto-releases when the document is hidden. Re-acquire when
		// the tab becomes visible again *if* audio is still actively playing.
		if (document.visibilityState === 'visible' && this.current && !this.audio.paused) {
			this.acquireWakeLock();
		}
	};

	// Holds the active screen wake lock sentinel (WakeLockSentinel) when playback
	// is in flight. Typed as any to avoid lib-dom version drift; runtime-guarded.
	private wakeLock: any = null;
	private mediaSessionHandlersRegistered = false;
	private metadata: AudioMetadataInit | null = null;

	id: string | null = null;
	onStopped: OnStoppedCallback | null = null;

	constructor(audioElement: HTMLAudioElement) {
		this.audio = audioElement;
		this.audio.addEventListener('ended', this._onEnded);
		try {
			document.addEventListener('visibilitychange', this._onVisibilityChange);
		} catch {}
	}

	setId(newId: string) {
		if (this.id === newId) return;

		this.#halt();
		this.id = newId;
		this.onStopped?.({ event: 'id-change', id: newId });
	}

	setPlaybackRate(rate: number) {
		this.audio.playbackRate = rate;
	}

	/**
	 * Set Media Session metadata (lock-screen title / artist). Called by the
	 * UI before enqueueing chunks so the OS treats this as a real media
	 * session — which prevents mobile browsers from suspending the tab when
	 * the screen locks and exposes play/pause controls on the lock screen.
	 */
	setMetadata(metadata: AudioQueueMetadata) {
		this.metadata = {
			title: (metadata.title || '').slice(0, 200) || 'Speaking',
			artist: metadata.artist || 'Open WebUI',
			album: metadata.album || ''
		};
		this.applyMediaSessionMetadata();
	}

	enqueue(url: string) {
		this.queue.push(url);

		// Auto-play if nothing is currently playing or loaded
		if (this.audio.paused && !this.current) {
			this.next();
		}
	}

	play() {
		if (!this.current && this.queue.length > 0) {
			this.next();
		} else {
			this.audio.play();
			this.updatePlaybackState('playing');
		}
	}

	next() {
		this.current = this.queue.shift() ?? null;

		if (this.current) {
			this.audio.src = this.current;
			this.audio.play();
			this.registerMediaSessionHandlers();
			this.applyMediaSessionMetadata();
			this.acquireWakeLock();
			this.updatePlaybackState('playing');
		} else {
			this.#halt();
			this.onStopped?.({ event: 'empty-queue', id: this.id });
		}
	}

	stop() {
		this.#halt();
		this.onStopped?.({ event: 'stop', id: this.id });
	}

	destroy() {
		this.audio.removeEventListener('ended', this._onEnded);
		try {
			document.removeEventListener('visibilitychange', this._onVisibilityChange);
		} catch {}
		this.#halt();
		this.clearMediaSessionHandlers();
		this.onStopped = null;
	}

	/**
	 * Pause audio and clear queue without firing onStopped.
	 * Callers that need the callback should invoke it themselves.
	 */
	#halt() {
		this.audio.pause();
		this.audio.currentTime = 0;
		this.audio.removeAttribute('src');
		this.audio.load();
		this.queue = [];
		this.current = null;
		this.metadata = null;
		this.releaseWakeLock();
		this.updatePlaybackState('none');
		this.clearMediaSessionMetadata();
	}

	private registerMediaSessionHandlers() {
		if (this.mediaSessionHandlersRegistered) return;
		if (typeof navigator === 'undefined' || !('mediaSession' in navigator)) return;

		try {
			navigator.mediaSession.setActionHandler('play', () => {
				try {
					this.audio.play();
					this.updatePlaybackState('playing');
				} catch {}
			});
			navigator.mediaSession.setActionHandler('pause', () => {
				try {
					this.audio.pause();
					this.updatePlaybackState('paused');
				} catch {}
			});
			navigator.mediaSession.setActionHandler('stop', () => {
				this.stop();
			});
			navigator.mediaSession.setActionHandler('seekbackward', (details: any) => {
				const skip = details?.seekOffset || 10;
				try {
					this.audio.currentTime = Math.max(this.audio.currentTime - skip, 0);
				} catch {}
			});
			navigator.mediaSession.setActionHandler('seekforward', (details: any) => {
				const skip = details?.seekOffset || 10;
				try {
					const duration = isFinite(this.audio.duration) ? this.audio.duration : 0;
					const target = this.audio.currentTime + skip;
					this.audio.currentTime = duration > 0 ? Math.min(target, duration) : target;
				} catch {}
			});
			this.mediaSessionHandlersRegistered = true;
		} catch {}
	}

	private clearMediaSessionHandlers() {
		if (!this.mediaSessionHandlersRegistered) return;
		if (typeof navigator === 'undefined' || !('mediaSession' in navigator)) return;
		try {
			for (const action of MEDIA_SESSION_ACTIONS) {
				try {
					navigator.mediaSession.setActionHandler(action as any, null);
				} catch {}
			}
		} catch {}
		this.mediaSessionHandlersRegistered = false;
	}

	private applyMediaSessionMetadata() {
		if (!this.metadata) return;
		if (typeof navigator === 'undefined' || !('mediaSession' in navigator)) return;
		if (typeof MediaMetadata === 'undefined') return;
		try {
			navigator.mediaSession.metadata = new MediaMetadata({
				title: this.metadata.title,
				artist: this.metadata.artist,
				album: this.metadata.album
			});
		} catch {}
	}

	private clearMediaSessionMetadata() {
		if (typeof navigator === 'undefined' || !('mediaSession' in navigator)) return;
		try {
			navigator.mediaSession.metadata = null;
		} catch {}
	}

	private updatePlaybackState(state: 'playing' | 'paused' | 'none') {
		if (typeof navigator === 'undefined' || !('mediaSession' in navigator)) return;
		try {
			navigator.mediaSession.playbackState = state;
		} catch {}
	}

	private acquireWakeLock() {
		if (this.wakeLock) return;
		if (typeof navigator === 'undefined') return;
		const nav: any = navigator;
		if (!nav.wakeLock || typeof nav.wakeLock.request !== 'function') return;
		// Fire-and-forget: failures are non-fatal (e.g. document not visible).
		nav.wakeLock
			.request('screen')
			.then((sentinel: any) => {
				this.wakeLock = sentinel;
				try {
					sentinel.addEventListener('release', () => {
						if (this.wakeLock === sentinel) {
							this.wakeLock = null;
						}
					});
				} catch {}
			})
			.catch(() => {});
	}

	private releaseWakeLock() {
		const sentinel = this.wakeLock;
		this.wakeLock = null;
		if (!sentinel) return;
		try {
			sentinel.release?.()?.catch?.(() => {});
		} catch {}
	}
}

interface AudioMetadataInit {
	title: string;
	artist: string;
	album: string;
}

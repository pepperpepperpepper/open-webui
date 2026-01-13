<script lang="ts">
	import { getContext } from 'svelte';

	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import Bolt from '$lib/components/icons/Bolt.svelte';
	import { chatId } from '$lib/stores';

	export let selectedModelId: string | null = null;
	export let className =
		'text-gray-600 dark:text-gray-300 hover:text-gray-700 dark:hover:text-gray-200 transition rounded-full p-1.5 self-center';

	const i18n = getContext('i18n');

	const normalizeLiveKitLlmModel = (modelId: string | null): string | null => {
		const lowered = (modelId || '').trim().toLowerCase();
		if (!lowered) return null;
		if (lowered.includes('4.6')) return 'glm-4.6';
		if (lowered.includes('4.7')) return 'glm-4.7';
		return null;
	};

	const openLiveKit = () => {
		const id = ($chatId || '').trim();
		if (!id) return;

		const room = `owui-voice-${id}`;
		const params = new URLSearchParams({
			room,
			chat_id: id,
			src: 'webui'
		});

		const llmModel = normalizeLiveKitLlmModel(selectedModelId);
		if (llmModel) {
			params.set('llm_model', llmModel);
		}

		window.open(`/livekit/?${params.toString()}`, '_blank', 'noopener,noreferrer');
	};
</script>

<Tooltip content={$i18n.t('Send to LiveKit')}>
	<button
		type="button"
		class={className}
		disabled={!$chatId}
		on:click={() => {
			openLiveKit();
		}}
		aria-label={$i18n.t('Send to LiveKit')}
	>
		<Bolt className="size-5" strokeWidth="2.25" />
	</button>
</Tooltip>

document.addEventListener('alpine:init', () => {
    Alpine.data('downloadsPanel', () => ({
        expanded: false,
        selectedVideoItag: '',
        selectedAudioItag: '',

        init() {
            this.$watch('$store.watch.videoData', () => {
                this.selectedVideoItag = '';
                this.selectedAudioItag = '';
            });
        },

        get combinedFormats() {
            const data = Alpine.store('watch').videoData;
            if (!data?.formatStreams) return [];
            return data.formatStreams;
        },

        get videoOnlyFormats() {
            const data = Alpine.store('watch').videoData;
            if (!data?.adaptiveFormats) return [];
            return data.adaptiveFormats
                .filter(f => f.type && f.type.startsWith('video/'))
                .sort((a, b) => (b.height || 0) - (a.height || 0));
        },

        get audioOnlyFormats() {
            const data = Alpine.store('watch').videoData;
            if (!data?.adaptiveFormats) return [];
            return data.adaptiveFormats
                .filter(f => f.type && f.type.startsWith('audio/'))
                .sort((a, b) => {
                    const bitrateA = parseInt(a.bitrate) || 0;
                    const bitrateB = parseInt(b.bitrate) || 0;
                    return bitrateB - bitrateA;
                });
        },

        get selectedVideoFormat() {
            return this.videoOnlyFormats.find(f => f.itag === this.selectedVideoItag) || null;
        },

        get selectedAudioFormat() {
            return this.audioOnlyFormats.find(f => f.itag === this.selectedAudioItag) || null;
        },

        get canMergeFormats() {
            return Boolean(this.selectedVideoFormat && this.selectedAudioFormat);
        },

        getMergedExtension() {
            const videoExt = (this.selectedVideoFormat?.container || '').toLowerCase();
            const audioExt = (this.selectedAudioFormat?.container || '').toLowerCase();

            if (videoExt === 'mp4' && ['m4a', 'mp4'].includes(audioExt)) return 'mp4';
            if (videoExt === 'webm' && ['webm', 'opus'].includes(audioExt)) return 'webm';
            return 'mkv';
        },

        getMergedDownloadUrl() {
            const data = Alpine.store('watch').videoData;
            const video = this.selectedVideoFormat;
            if (!data?.videoId || !video || !this.selectedAudioFormat) return '';

            // Preserve only Yattee's download parameters from an existing fast
            // URL. Direct media URLs contain many upstream query parameters that
            // must not be copied onto our endpoint.
            const sourceUrl = new URL(video.url, window.location.origin);
            const sourceIsFastDownload = sourceUrl.pathname.includes('/proxy/fast/');
            const sourceParams = new URLSearchParams(sourceUrl.search);
            const params = new URLSearchParams();
            if (sourceIsFastDownload && sourceParams.has('url')) {
                params.set('url', sourceParams.get('url'));
            }
            if (sourceIsFastDownload && sourceParams.has('token')) {
                params.set('token', sourceParams.get('token'));
            }
            params.set('video_itag', video.itag);
            params.set('audio_itag', this.selectedAudioFormat.itag);
            if (!params.has('url') && data.originalUrl) {
                params.set('url', data.originalUrl);
            }
            if (!params.has('token') && data.downloadToken) {
                params.set('token', data.downloadToken);
            }

            const path = sourceIsFastDownload
                ? sourceUrl.pathname
                : `/proxy/fast/${encodeURIComponent(data.videoId)}`;
            return `${path}?${params.toString()}`;
        },

        getMergedFilename() {
            const data = Alpine.store('watch').videoData;
            const title = data?.title || 'video';
            const safeTitle = title.replace(/[/\\?%*:|"<>]/g, '-').substring(0, 100);
            const quality = this.selectedVideoFormat?.resolution || this.selectedVideoFormat?.quality || '';
            return `${safeTitle}${quality ? '_' + quality : ''}.${this.getMergedExtension()}`;
        },

        videoOptionLabel(format) {
            const parts = [format.resolution || format.quality, format.encoding || format.container];
            if (format.fps > 30) parts.push(`${format.fps}fps`);
            return parts.filter(Boolean).join(' · ');
        },

        audioOptionLabel(format) {
            const bitrate = parseInt(format.bitrate);
            const quality = format.audioQuality || (!isNaN(bitrate) ? `${Math.round(bitrate / 1000)} kbps` : 'Audio');
            const language = format.audioTrack?.displayName;
            return [quality, format.container, language].filter(Boolean).join(' · ');
        },

        downloadMerged() {
            if (!this.canMergeFormats) return;

            const link = document.createElement('a');
            link.href = this.getMergedDownloadUrl();
            link.download = this.getMergedFilename();
            document.body.appendChild(link);
            link.click();
            link.remove();
        },

        getFilename(format) {
            const data = Alpine.store('watch').videoData;
            const title = data?.title || 'video';
            const safeTitle = title.replace(/[/\\?%*:|"<>]/g, '-').substring(0, 100);
            const quality = format.resolution || format.quality || format.audioQuality || '';
            const ext = format.container || 'mp4';
            return `${safeTitle}${quality ? '_' + quality : ''}.${ext}`;
        },

        formatBytes(bytes) {
            if (!bytes) return '';
            const num = parseInt(bytes);
            if (isNaN(num)) return '';
            if (num >= 1073741824) {
                return (num / 1073741824).toFixed(1) + ' GB';
            }
            if (num >= 1048576) {
                return (num / 1048576).toFixed(1) + ' MB';
            }
            if (num >= 1024) {
                return (num / 1024).toFixed(1) + ' KB';
            }
            return num + ' B';
        }
    }));
});

import math
from typing import Text, Union
from pathlib import Path

import librosa
import torch
import torchaudio
from torch import Tensor

AudioFile = Union[Path, Text, dict]
"""
Audio files can be provided to the Audio class using different types:
    - a "str" instance: "/path/to/audio.wav"
    - a "Path" instance: Path("/path/to/audio.wav")
    - a dict with a mandatory "audio" key (mandatory) and an optional "channel" key:
        {"audio": "/path/to/audio.wav", "channel": 0}
    - a dict with mandatory "samples" and "sample_rate" keys and an optional "channel" key:
        {"samples": (channel, time) torch.Tensor, "sample_rate": 44100}

The optional "channel" key can be used to indicate a specific channel.
"""

# TODO: Remove this when it is the default
torchaudio.USE_SOUNDFILE_LEGACY_INTERFACE = False
torchaudio.set_audio_backend("soundfile")


class Audio:
    """Audio IO with on-the-fly resampling

    Parameters
    ----------
    sample_rate: int
        Target sample rate. 
    mono : int, optional
        Convert multi-channel to mono. Defaults to True.

    Usage
    -----
    >>> audio = Audio(sample_rate=16000)
    >>> samples = audio("/path/to/audio.wav")

    # on-the-fly resampling
    >>> original_sample_rate = 44100
    >>> two_seconds_stereo = torch.rand(2, 2 * original_sample_rate)
    >>> samples = audio({"samples": two_seconds_stereo, "sample_rate": original_sample_rate})
    >>> assert samples.shape[1] == 2 * 16000
    """

    @staticmethod
    def is_valid(file: AudioFile) -> bool:

        if isinstance(file, dict):

            if "samples" in file:

                samples = file["samples"]
                if len(samples.shape) != 2 or samples.shape[0] > samples.shape[1]:
                    raise ValueError(
                        "'samples' must be provided as a (channel, time) torch.Tensor."
                    )

                sample_rate = file.get("sample_rate", None)
                if sample_rate is None:
                    raise ValueError(
                        "'samples' must be provided with their 'sample_rate'."
                    )
                return True

            elif "audio" in file:
                return True

            else:
                # TODO improve error message
                raise ValueError("either 'audio' or 'samples' key must be provided.")

        return True

    @staticmethod
    def rms_normalize(samples: Tensor) -> Tensor:
        """Power-normalize samples

        Parameters
        ----------
        samples : (channel, time) Tensor
            Single or multichannel samples

        Returns
        -------
        samples: (channel, time) Tensor
            Power-normalized samples
        """
        rms = samples.square().mean(dim=1).sqrt()
        return (samples.t() / (rms + 1e-8)).t()

    def get_num_samples(self, file: AudioFile) -> int:
        """Number of samples (in target sample rate)

        :param file: audio file

        """

        self.is_valid(file)

        if isinstance(file, dict):

            # file = {"samples": torch.Tensor, "sample_rate": int, [ "channel": int ]}
            if "samples" in file:
                num_samples = file["samples"].shape[1]
                sample_rate = file["sample_rate"]

            # file = {"audio": str or Path, [ "channel": int ]}
            else:
                info = torchaudio.info(file["audio"])
                num_samples = info.num_frames
                sample_rate = info.sample_rate

        #  file = str or Path
        else:
            info = torchaudio.info(file)
            num_samples = info.num_frames
            sample_rate = info.sample_rate

        return num_samples * self.sample_rate / sample_rate

    def __init__(self, sample_rate: int, mono: bool = True):
        super().__init__()
        self.sample_rate = sample_rate
        self.mono = mono

    def downmix_and_resample(self, samples: Tensor, sample_rate: int) -> Tensor:
        """Downmix and resample

        Parameters
        ----------
        samples : (channel, time) Tensor
            Samples.
        sample_rate : int
            Original sample rate.

        Returns
        -------
        samples : (channel, time) Tensor
            Remixed and resampled samples
        """

        # downmix to mono
        if self.mono and samples.shape[0] > 1:
            samples = samples.mean(dim=0, keepdim=True)

        # resample
        if self.sample_rate != sample_rate:
            samples = samples.numpy()
            if self.mono:
                # librosa expects mono audio to be of shape (n,), but we have (1, n).
                samples = librosa.core.resample(
                    samples[0], sample_rate, self.sample_rate
                )[None]
            else:
                samples = librosa.core.resample(
                    samples.T, sample_rate, self.sample_rate
                ).T
            sample_rate = self.sample_rate
            samples = torch.tensor(samples)

        return samples

    def __call__(
        self, file: AudioFile, sample_offset: int = 0, num_samples: int = None,
    ) -> Tensor:
        """

        Parameters
        ----------
        file : AudioFile
            Audio file.
        sample_offset : int, optional
            Start loading at this `sample_offset` sample. Defaults ot 0.
        num_samples : int, optional
            Load that many samples. Defaults to load up to the end of the file.

        Returns
        -------
        samples : (time, channel) torch.Tensor
            Samples

        """

        self.is_valid(file)

        original_samples = None

        if isinstance(file, dict):

            # file = {"samples": torch.Tensor, "sample_rate": int, [ "channel": int ]}
            if "samples" in file:
                original_samples = file["samples"]
                original_sample_rate = file["sample_rate"]
                original_total_num_samples = original_samples.shape[1]
                channel = file.get("channel", None)

            # file = {"audio": str or Path, [ "channel": int ]}
            else:
                audio_path = str(file["audio"])
                info = torchaudio.info(audio_path)
                original_sample_rate = info.sample_rate
                original_total_num_samples = info.num_frames
                channel = file.get("channel", None)

        #  file = str or Path
        else:
            audio_path = str(file)
            info = torchaudio.info(audio_path)
            original_sample_rate = info.sample_rate
            original_total_num_samples = info.num_frames
            channel = None

        original_sample_offset = round(
            sample_offset * original_sample_rate / self.sample_rate
        )
        if num_samples is None:
            original_num_samples = original_total_num_samples - original_sample_offset
        else:
            original_num_samples = round(
                num_samples * original_sample_rate / self.sample_rate
            )

        if original_sample_offset + original_num_samples > original_total_num_samples:
            raise ValueError()

        if original_samples is None:
            original_data, _ = torchaudio.load(
                audio_path,
                frame_offset=original_sample_offset,
                num_frames=original_num_samples,
            )

        else:
            original_data = original_samples[
                :, original_sample_offset : original_sample_offset + original_num_samples
            ]

        if channel is not None:
            original_data = original_data[channel - 1 : channel, :]

        return self.downmix_and_resample(original_data, original_sample_rate)
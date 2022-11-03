from __future__ import annotations

import abc
import warnings
from typing import Any
from collections import namedtuple
from numbers import Real

import numpy as np
import deepdish as dd
from lmfit import Parameters, minimize, report_fit
from lmfit.minimizer import MinimizerResult
import sep

from psfpy.psf import SimplePSF, VariedPSF, PointSpreadFunctionABC
from psfpy.exceptions import InvalidSizeError


class PatchCollectionABC(metaclass=abc.ABCMeta):
    def __init__(self, patches: dict[Any, np.ndarray]):
        self._patches = patches
        if patches:
            shape = next(iter(patches.values())).shape
            # TODO: check that the patches are square
            self._size = shape[0]
        else:
            self._size = None

    def __len__(self):
        return len(self._patches)

    @classmethod
    @abc.abstractmethod
    def extract(cls, images: list[np.ndarray], coordinates: list, size: int) -> PatchCollectionABC:
        """

        Parameters
        ----------
        images
        coordinates
        size

        Returns
        -------

        """

    def __getitem__(self, identifier) -> np.ndarray:
        """

        Parameters
        ----------
        identifier

        Returns
        -------

        """
        if identifier in self._patches:
            return self._patches[identifier]
        else:
            raise IndexError(f"{identifier} is not used to identify a patch in this collection.")

    def __contains__(self, identifier):
        """

        Parameters
        ----------
        identifier

        Returns
        -------

        """
        return identifier in self._patches

    def add(self, identifier, patch: np.ndarray) -> None:
        """

        Parameters
        ----------
        identifier
        patch

        Returns
        -------

        """
        if identifier in self._patches:
            # TODO: improve warning
            warnings.warn(f"{identifier} is being overwritten in this collection.", Warning)
        self._patches[identifier] = patch

        if self._size is None:
            self._size = patch.shape[0]
            # TODO : enforce square constraint

    @abc.abstractmethod
    def average(self, corners: np.ndarray, step: int, size: int, mode: str) -> PatchCollectionABC:
        """

        Parameters
        ----------
        corners
        step
        size

        Returns
        -------

        """

    @abc.abstractmethod
    def fit(self, base_psf: SimplePSF, is_varied: bool = False) -> PointSpreadFunctionABC:
        """

        Parameters
        ----------
        base_psf
        is_varied

        Returns
        -------

        """

    def save(self, path):
        dd.io.save(path, self._patches)

    @classmethod
    def load(cls, path):
        return cls(dd.io.load(path))

    def keys(self):
        return self._patches.keys()

    def values(self):
        return self._patches.values()

    def items(self):
        return self._patches.items()

    def __next__(self):
        # TODO: implement
        pass

    def _fit_lmfit(self, base_psf: SimplePSF, initial_guesses: dict[str, Real]) -> dict[Any, MinimizerResult]:
        initial = Parameters()
        for parameter in base_psf.parameters:
            initial.add(parameter, value=initial_guesses[parameter])

        xx, yy = np.meshgrid(np.arange(self._size), np.arange(self._size))

        results = dict()
        for identifier, patch in self._patches.items():
            results[identifier] = minimize(
                lambda current_parameters, x, y, data: data - base_psf(x, y, **current_parameters.valuesdict()),
                initial,
                args=(xx, yy, patch))
        return results


CoordinateIdentifier = namedtuple("CoordinateIdentifier", "image_index, x, y")


class CoordinatePatchCollection(PatchCollectionABC):
    @classmethod
    def extract(cls, images: list[np.ndarray],
                coordinates: list[CoordinateIdentifier],
                size: int) -> PatchCollectionABC:
        out = cls(dict())

        # pad in case someone selects a region on the edge of the image
        padding_shape = ((size, size), (size, size))
        padded_images = [np.pad(image, padding_shape, mode='constant') for image in images]

        # TODO: prevent someone from selecting a region completing outside of the image
        for coordinate in coordinates:
            patch = padded_images[coordinate.image_index][coordinate.x+size:coordinate.x+2*size,
                                                          coordinate.y+size:coordinate.y+2*size]
            out.add(coordinate, patch)
        return out

    @classmethod
    def find_stars_and_create(cls, images: list[np.ndarray], patch_size: int, star_threshold: int = 3):
        coordinates = []
        for i, image in enumerate(images):
            background = sep.Background(image)
            image_background_removed = image - background
            image_star_coords = sep.extract(image_background_removed, star_threshold, err=background.globalrms)
            coordinates += [CoordinateIdentifier(i, int(y-patch_size/2), int(x-patch_size/2))
                            for x, y in zip(image_star_coords['x'], image_star_coords['y'])]
        return cls.extract(images, coordinates, patch_size)

    def average(self, corners: np.ndarray, step: int, size: int,
                mode: str = "median") -> PatchCollectionABC:
        self._validate_average_mode(mode)
        pad_shape = self._calculate_pad_shape(size)

        if mode == "mean":
            mean_stack = {tuple(corner): np.zeros((size, size)) for corner in corners}
            counts = {tuple(corner): 0 for corner in corners}
        elif mode == "median":
            median_stack = {tuple(corner): [] for corner in corners}

        corners_x, corners_y = corners[:, 0], corners[:, 1]
        x_bounds = np.stack([corners_x, corners_x + step], axis=-1)
        y_bounds = np.stack([corners_y, corners_y + step], axis=-1)

        for identifier, patch in self._patches.items():
            # pad patch with zeros
            padded_patch = np.pad(patch / np.max(patch), pad_shape, mode='constant')

            # Determine which average region it belongs to
            center_x = identifier.x + self._size // 2
            center_y = identifier.y + self._size // 2
            x_matches = (x_bounds[:, 0] <= center_x) * (center_x < x_bounds[:, 1])
            y_matches = (y_bounds[:, 0] <= center_y) * (center_y < y_bounds[:, 1])
            match_indices = np.where(x_matches * y_matches)[0]

            # add to averages and increment count
            for match_index in match_indices:
                match_corner = tuple(corners[match_index])
                if mode == "mean":
                    mean_stack[match_corner] = np.nansum([mean_stack[match_corner], padded_patch], axis=0)
                    counts[match_corner] += 1
                elif mode == "median":
                    median_stack[match_corner].append(padded_patch)

        if mode == "mean":
            averages = {CoordinateIdentifier(None, corner[0], corner[1]): mean_stack[corner]/counts[corner]
                        for corner in mean_stack}
        elif mode == "median":
            averages = {CoordinateIdentifier(None, corner[0], corner[1]):
                            np.nanmedian(median_stack[corner], axis=0)
                            if len(median_stack[corner]) > 0 else np.zeros((size, size))
                        for corner in median_stack}
        return CoordinatePatchCollection(averages)

    def _validate_average_mode(self, mode: str):
        valid_modes = ['median', 'mean']
        if mode not in valid_modes:
            raise ValueError(f"Found a mode of {mode} but it must be in the list {valid_modes}.")

    def _calculate_pad_shape(self, size):
        pad_amount = size - self._size
        if pad_amount < 0:
            raise InvalidSizeError(f"The average window size (found {size})" 
                                   f"must be larger than the existing patch size (found {self._size}).")
        if pad_amount % 2 != 0:
            raise InvalidSizeError(f"The average window size (found {size})" 
                                   f"must be the same parity as the existing patch size (found {self._size}).")
        pad_shape = ((pad_amount//2, pad_amount//2), (pad_amount//2, pad_amount//2))
        return pad_shape

    def fit(self, base_psf: SimplePSF, is_varied: bool = False) -> PointSpreadFunctionABC:
        raise NotImplementedError("TODO")  # TODO: implement


